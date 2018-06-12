# Copyright 2018 Xiaomi, Inc.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import falcon_cli
import filelock
import glob
import logging
import numpy as np
import os
import re
import sh
import struct
import subprocess
import sys
import time
import urllib
from enum import Enum

import common

sys.path.insert(0, "mace/python/tools")
try:
    from encrypt_opencl_codegen import encrypt_opencl_codegen
    from binary_codegen import tuning_param_codegen
    from generate_data import generate_input_data
    from validate import validate
    from mace_engine_factory_codegen import gen_mace_engine_factory
except Exception as e:
    print("Import error:\n%s" % e)
    exit(1)

################################
# common
################################


def strip_invalid_utf8(str):
    return sh.iconv(str, "-c", "-t", "UTF-8")


def split_stdout(stdout_str):
    stdout_str = strip_invalid_utf8(stdout_str)
    # Filter out last empty line
    return [l.strip() for l in stdout_str.split('\n') if len(l.strip()) > 0]


def make_output_processor(buff):
    def process_output(line):
        print(line.rstrip())
        buff.append(line)

    return process_output


def device_lock_path(serialno):
    return "/tmp/device-lock-%s" % serialno


def device_lock(serialno, timeout=3600):
    return filelock.FileLock(device_lock_path(serialno), timeout=timeout)


def is_device_locked(serialno):
    try:
        with device_lock(serialno, timeout=0.000001):
            return False
    except filelock.Timeout:
        return True


class BuildType(object):
    proto = 'proto'
    code = 'code'


def stdout_success(stdout):
    stdout_lines = stdout.split("\n")
    for line in stdout_lines:
        if "Aborted" in line or "FAILED" in line or \
                        "Segmentation fault" in line:
            return False
    return True


################################
# clear data
################################
def clear_phone_data_dir(serialno, phone_data_dir):
    sh.adb("-s",
           serialno,
           "shell",
           "rm -rf %s" % phone_data_dir)


def clear_model_codegen(model_codegen_dir="mace/codegen/models"):
    if os.path.exists(model_codegen_dir):
        sh.rm("-rf", model_codegen_dir)


################################
# adb commands
################################
def adb_devices():
    serialnos = []
    p = re.compile(r'(\w+)\s+device')
    for line in split_stdout(sh.adb("devices")):
        m = p.match(line)
        if m:
            serialnos.append(m.group(1))

    return serialnos


def get_soc_serialnos_map():
    serialnos = adb_devices()
    soc_serialnos_map = {}
    for serialno in serialnos:
        props = adb_getprop_by_serialno(serialno)
        soc_serialnos_map.setdefault(props["ro.board.platform"], [])\
            .append(serialno)

    return soc_serialnos_map


def get_target_socs_serialnos(target_socs=None):
    soc_serialnos_map = get_soc_serialnos_map()
    serialnos = []
    if target_socs is None:
        target_socs = soc_serialnos_map.keys()
    for target_soc in target_socs:
        serialnos.extend(soc_serialnos_map[target_soc])
    return serialnos


def get_soc_serial_number_map():
    serial_numbers = adb_devices()
    soc_serial_number_map = {}
    for num in serial_numbers:
        props = adb_getprop_by_serialno(num)
        soc_serial_number_map[props["ro.board.platform"]] = num
    return soc_serial_number_map


def get_target_soc_serial_number(target_soc):
    soc_serial_number_map = get_soc_serial_number_map()
    serial_number = None
    if target_soc in soc_serial_number_map:
        serial_number = soc_serial_number_map[target_soc]
    return serial_number


def adb_getprop_by_serialno(serialno):
    outputs = sh.adb("-s", serialno, "shell", "getprop")
    raw_props = split_stdout(outputs)
    props = {}
    p = re.compile(r'\[(.+)\]: \[(.+)\]')
    for raw_prop in raw_props:
        m = p.match(raw_prop)
        if m:
            props[m.group(1)] = m.group(2)
    return props


def adb_get_device_name_by_serialno(serialno):
    props = adb_getprop_by_serialno(serialno)
    return props.get("ro.product.model", "").replace(' ', '')


def adb_supported_abis(serialno):
    props = adb_getprop_by_serialno(serialno)
    abilist_str = props["ro.product.cpu.abilist"]
    abis = [abi.strip() for abi in abilist_str.split(',')]
    return abis


def adb_get_all_socs():
    socs = []
    for d in adb_devices():
        props = adb_getprop_by_serialno(d)
        socs.append(props["ro.board.platform"])
    return set(socs)


def adb_push(src_path, dst_path, serialno):
    print("Push %s to %s" % (src_path, dst_path))
    sh.adb("-s", serialno, "push", src_path, dst_path)


def adb_pull(src_path, dst_path, serialno):
    print("Pull %s to %s" % (src_path, dst_path))
    try:
        sh.adb("-s", serialno, "pull", src_path, dst_path)
    except Exception as e:
        print("Error msg: %s" % e.stderr)


def adb_run(abi,
            serialno,
            host_bin_path,
            bin_name,
            args="",
            opencl_profiling=True,
            vlog_level=0,
            device_bin_path="/data/local/tmp/mace",
            out_of_range_check=True,
            address_sanitizer=False):
    host_bin_full_path = "%s/%s" % (host_bin_path, bin_name)
    device_bin_full_path = "%s/%s" % (device_bin_path, bin_name)
    props = adb_getprop_by_serialno(serialno)
    print(
        "====================================================================="
    )
    print("Trying to lock device %s" % serialno)
    with device_lock(serialno):
        print("Run on device: %s, %s, %s" %
              (serialno, props["ro.board.platform"],
               props["ro.product.model"]))
        sh.adb("-s", serialno, "shell", "rm -rf %s" % device_bin_path)
        sh.adb("-s", serialno, "shell", "mkdir -p %s" % device_bin_path)
        adb_push(host_bin_full_path, device_bin_full_path, serialno)
        ld_preload = ""
        if address_sanitizer:
            adb_push(find_asan_rt_library(abi), device_bin_path, serialno)
            ld_preload = "LD_PRELOAD=%s/%s" % (device_bin_path,
                                               asan_rt_library_names(abi)),
        opencl_profiling = 1 if opencl_profiling else 0
        out_of_range_check = 1 if out_of_range_check else 0
        print("Run %s" % device_bin_full_path)

        stdout_buff = []
        process_output = make_output_processor(stdout_buff)
        sh.adb(
            "-s",
            serialno,
            "shell",
            ld_preload,
            "MACE_OUT_OF_RANGE_CHECK=%d" % out_of_range_check,
            "MACE_OPENCL_PROFILING=%d" % opencl_profiling,
            "MACE_CPP_MIN_VLOG_LEVEL=%d" % vlog_level,
            device_bin_full_path,
            args,
            _tty_in=True,
            _out=process_output,
            _err_to_out=True)
        return "".join(stdout_buff)


################################
# Toolchain
################################
def asan_rt_library_names(abi):
    asan_rt_names = {
        "armeabi-v7a": "libclang_rt.asan-arm-android.so",
        "arm64-v8a": "libclang_rt.asan-aarch64-android.so",
    }
    return asan_rt_names[abi]


def find_asan_rt_library(abi, asan_rt_path=''):
    if not asan_rt_path:
        find_path = os.environ['ANDROID_NDK_HOME']
        candidates = split_stdout(sh.find(find_path, "-name",
                                          asan_rt_library_names(abi)))
        if len(candidates) == 0:
            common.MaceLogger.error(
                "Toolchain",
                "Can't find AddressSanitizer runtime library in % s" %
                find_path)
        elif len(candidates) > 1:
            common.MaceLogger.info(
                "More than one AddressSanitizer runtime library, use the 1st")
        return candidates[0]
    return "%s/%s" % (asan_rt_path, asan_rt_library_names(abi))


################################
# bazel commands
################################
def bazel_build(target,
                abi="armeabi-v7a",
                hexagon_mode=False,
                enable_openmp=True,
                enable_neon=True,
                address_sanitizer=False):
    print("* Build %s with ABI %s" % (target, abi))
    if abi == "host":
        bazel_args = (
            "build",
            "--define",
            "openmp=%s" % str(enable_openmp).lower(),
            target,
        )
    else:
        bazel_args = (
            "build",
            target,
            "--config",
            "android",
            "--cpu=%s" % abi,
            "--define",
            "neon=%s" % str(enable_neon).lower(),
            "--define",
            "openmp=%s" % str(enable_openmp).lower(),
            "--define",
            "hexagon=%s" % str(hexagon_mode).lower())
    if address_sanitizer:
        bazel_args += ("--config", "asan")
    else:
        bazel_args += ("--config", "optimization")
    sh.bazel(
        _fg=True,
        *bazel_args)
    print("Build done!\n")


def bazel_build_common(target, build_args=""):
    stdout_buff = []
    process_output = make_output_processor(stdout_buff)
    sh.bazel(
        "build",
        target + build_args,
        _tty_in=True,
        _out=process_output,
        _err_to_out=True)
    return "".join(stdout_buff)


def bazel_target_to_bin(target):
    # change //mace/a/b:c to bazel-bin/mace/a/b/c
    prefix, bin_name = target.split(':')
    prefix = prefix.replace('//', '/')
    if prefix.startswith('/'):
        prefix = prefix[1:]
    host_bin_path = "bazel-bin/%s" % prefix
    return host_bin_path, bin_name


################################
# mace commands
################################
def gen_encrypted_opencl_source(codegen_path="mace/codegen"):
    sh.mkdir("-p", "%s/opencl" % codegen_path)
    encrypt_opencl_codegen("./mace/kernels/opencl/cl/",
                           "mace/codegen/opencl/opencl_encrypt_program.cc")


def gen_mace_engine_factory_source(model_tags,
                                   model_load_type,
                                   codegen_path="mace/codegen"):
    print("* Genearte mace engine creator source")
    codegen_tools_dir = "%s/engine" % codegen_path
    sh.rm("-rf", codegen_tools_dir)
    sh.mkdir("-p", codegen_tools_dir)
    gen_mace_engine_factory(
        model_tags,
        "mace/python/tools",
        model_load_type,
        codegen_tools_dir)
    print("Genearte mace engine creator source done!\n")


def pull_binaries(abi, serialno, model_output_dirs,
                  cl_built_kernel_file_name):
    compiled_opencl_dir = "/data/local/tmp/mace_run/interior/"
    mace_run_param_file = "mace_run.config"

    cl_bin_dirs = []
    for d in model_output_dirs:
        cl_bin_dirs.append(os.path.join(d, "opencl_bin"))
    cl_bin_dirs_str = ",".join(cl_bin_dirs)
    if cl_bin_dirs:
        cl_bin_dir = cl_bin_dirs_str
        if os.path.exists(cl_bin_dir):
            sh.rm("-rf", cl_bin_dir)
        sh.mkdir("-p", cl_bin_dir)
        if abi != "host":
            adb_pull(compiled_opencl_dir + cl_built_kernel_file_name,
                     cl_bin_dir, serialno)
            adb_pull("/data/local/tmp/mace_run/%s" % mace_run_param_file,
                     cl_bin_dir, serialno)


def merge_opencl_binaries(binaries_dirs,
                          cl_compiled_program_file_name,
                          output_file_path):
    platform_info_key = 'mace_opencl_precompiled_platform_info_key'
    cl_bin_dirs = []
    for d in binaries_dirs:
        cl_bin_dirs.append(os.path.join(d, "opencl_bin"))
    # create opencl binary output dir
    opencl_binary_dir = os.path.dirname(output_file_path)
    if not os.path.exists(opencl_binary_dir):
        sh.mkdir("-p", opencl_binary_dir)
    kvs = {}
    for binary_dir in cl_bin_dirs:
        binary_path = os.path.join(binary_dir, cl_compiled_program_file_name)
        if not os.path.exists(binary_path):
            continue

        print 'generate opencl code from', binary_path
        with open(binary_path, "rb") as f:
            binary_array = np.fromfile(f, dtype=np.uint8)

        idx = 0
        size, = struct.unpack("Q", binary_array[idx:idx + 8])
        idx += 8
        for _ in xrange(size):
            key_size, = struct.unpack("i", binary_array[idx:idx + 4])
            idx += 4
            key, = struct.unpack(
                str(key_size) + "s", binary_array[idx:idx + key_size])
            idx += key_size
            value_size, = struct.unpack("i", binary_array[idx:idx + 4])
            idx += 4
            if key == platform_info_key and key in kvs:
                common.mace_check(
                    (kvs[key] == binary_array[idx:idx + value_size]).all(),
                    "",
                    "There exists more than one OpenCL version for models:"
                    " %s vs %s " %
                    (kvs[key], binary_array[idx:idx + value_size]))
            else:
                kvs[key] = binary_array[idx:idx + value_size]
            idx += value_size

    output_byte_array = bytearray()
    data_size = len(kvs)
    output_byte_array.extend(struct.pack("Q", data_size))
    for key, value in kvs.iteritems():
        key_size = len(key)
        output_byte_array.extend(struct.pack("i", key_size))
        output_byte_array.extend(struct.pack(str(key_size) + "s", key))
        value_size = len(value)
        output_byte_array.extend(struct.pack("i", value_size))
        output_byte_array.extend(value)

    np.array(output_byte_array).tofile(output_file_path)


def gen_tuning_param_code(model_output_dirs,
                          codegen_path="mace/codegen"):
    mace_run_param_file = "mace_run.config"
    cl_bin_dirs = []
    for d in model_output_dirs:
        cl_bin_dirs.append(os.path.join(d, "opencl_bin"))
    cl_bin_dirs_str = ",".join(cl_bin_dirs)

    tuning_codegen_dir = "%s/tuning/" % codegen_path
    if not os.path.exists(tuning_codegen_dir):
        sh.mkdir("-p", tuning_codegen_dir)

    tuning_param_variable_name = "kTuningParamsData"
    tuning_param_codegen(cl_bin_dirs_str,
                         mace_run_param_file,
                         "%s/tuning_params.cc" % tuning_codegen_dir,
                         tuning_param_variable_name)


def gen_mace_version(codegen_path="mace/codegen"):
    sh.mkdir("-p", "%s/version" % codegen_path)
    sh.bash("mace/tools/git/gen_version_source.sh",
            "%s/version/version.cc" % codegen_path)


def gen_model_code(model_codegen_dir,
                   platform,
                   model_file_path,
                   weight_file_path,
                   model_sha256_checksum,
                   weight_sha256_checksum,
                   input_nodes,
                   output_nodes,
                   runtime,
                   model_tag,
                   input_shapes,
                   dsp_mode,
                   embed_model_data,
                   fast_conv,
                   obfuscate,
                   model_build_type,
                   data_type):
    bazel_build_common("//mace/python/tools:converter")

    if os.path.exists(model_codegen_dir):
        sh.rm("-rf", model_codegen_dir)
    sh.mkdir("-p", model_codegen_dir)

    sh.python("bazel-bin/mace/python/tools/converter",
              "-u",
              "--platform=%s" % platform,
              "--model_file=%s" % model_file_path,
              "--weight_file=%s" % weight_file_path,
              "--model_checksum=%s" % model_sha256_checksum,
              "--weight_checksum=%s" % weight_sha256_checksum,
              "--input_node=%s" % input_nodes,
              "--output_node=%s" % output_nodes,
              "--runtime=%s" % runtime,
              "--template=%s" % "mace/python/tools",
              "--model_tag=%s" % model_tag,
              "--input_shape=%s" % input_shapes,
              "--dsp_mode=%s" % dsp_mode,
              "--embed_model_data=%s" % embed_model_data,
              "--winograd=%s" % fast_conv,
              "--obfuscate=%s" % obfuscate,
              "--output_dir=%s" % model_codegen_dir,
              "--model_build_type=%s" % model_build_type,
              "--data_type=%s" % data_type,
              _fg=True)


def gen_random_input(model_output_dir,
                     input_nodes,
                     input_shapes,
                     input_files,
                     input_file_name="model_input"):
    for input_name in input_nodes:
        formatted_name = common.formatted_file_name(
            input_file_name, input_name)
        if os.path.exists("%s/%s" % (model_output_dir, formatted_name)):
            sh.rm("%s/%s" % (model_output_dir, formatted_name))
    input_nodes_str = ",".join(input_nodes)
    input_shapes_str = ":".join(input_shapes)
    generate_input_data("%s/%s" % (model_output_dir, input_file_name),
                        input_nodes_str,
                        input_shapes_str)

    input_file_list = []
    if isinstance(input_files, list):
        input_file_list.extend(input_files)
    else:
        input_file_list.append(input_files)
    if len(input_file_list) != 0:
        input_name_list = []
        if isinstance(input_nodes, list):
            input_name_list.extend(input_nodes)
        else:
            input_name_list.append(input_nodes)
        if len(input_file_list) != len(input_name_list):
            raise Exception('If input_files set, the input files should '
                            'match the input names.')
        for i in range(len(input_file_list)):
            if input_file_list[i] is not None:
                dst_input_file = model_output_dir + '/' + \
                        common.formatted_file_name(input_file_name,
                                                   input_name_list[i])
                if input_file_list[i].startswith("http://") or \
                        input_file_list[i].startswith("https://"):
                    urllib.urlretrieve(input_file_list[i], dst_input_file)
                else:
                    sh.cp("-f", input_file_list[i], dst_input_file)


def update_mace_run_lib(build_tmp_binary_dir, linkshared=0):
    if linkshared == 0:
        mace_run_filepath = build_tmp_binary_dir + "/mace_run_static"
    else:
        mace_run_filepath = build_tmp_binary_dir + "/mace_run_shared"

    if os.path.exists(mace_run_filepath):
        sh.rm("-rf", mace_run_filepath)
    if linkshared == 0:
        sh.cp("-f", "bazel-bin/mace/tools/validation/mace_run_static",
              build_tmp_binary_dir)
    else:
        sh.cp("-f", "bazel-bin/mace/tools/validation/mace_run_shared",
              build_tmp_binary_dir)


def touch_tuned_file_flag(build_tmp_binary_dir):
    sh.touch(build_tmp_binary_dir + '/tuned')


def is_binary_tuned(build_tmp_binary_dir):
    return os.path.exists(build_tmp_binary_dir + '/tuned')


def create_internal_storage_dir(serialno, phone_data_dir):
    internal_storage_dir = "%s/interior/" % phone_data_dir
    sh.adb("-s", serialno, "shell", "mkdir", "-p", internal_storage_dir)
    return internal_storage_dir


def update_libmace_shared_library(serial_num,
                                  abi,
                                  project_name,
                                  build_output_dir,
                                  library_output_dir):
    libmace_name = "libmace.so"
    mace_library_dir = "./dynamic_lib/"
    library_dir = "%s/%s/%s/%s" % (
            build_output_dir, project_name, library_output_dir, abi)
    libmace_file = "%s/%s" % (library_dir, libmace_name)

    if os.path.exists(libmace_file):
        sh.rm("-rf", library_dir)
    sh.mkdir("-p", library_dir)
    sh.cp("-f", "bazel-bin/mace/libmace.so", library_dir)
    sh.cp("-f", "%s/%s/libgnustl_shared.so" % (mace_library_dir, abi),
          library_dir)

    libmace_load_path = "%s/%s" % (mace_library_dir, libmace_name)
    if os.path.exists(libmace_load_path):
        sh.rm("-f", libmace_load_path)
    sh.cp("-f", "bazel-bin/mace/libmace.so", mace_library_dir)


def tuning_run(abi,
               serialno,
               mace_run_dir,
               vlog_level,
               embed_model_data,
               model_output_dir,
               input_nodes,
               output_nodes,
               input_shapes,
               output_shapes,
               mace_model_dir,
               model_tag,
               device_type,
               running_round,
               restart_round,
               limit_opencl_kernel_time,
               tuning,
               out_of_range_check,
               phone_data_dir,
               build_type,
               opencl_binary_file,
               shared_library_dir,
               omp_num_threads=-1,
               cpu_affinity_policy=1,
               gpu_perf_hint=3,
               gpu_priority_hint=3,
               input_file_name="model_input",
               output_file_name="model_out",
               runtime_failure_ratio=0.0,
               address_sanitizer=False,
               linkshared=0):
    print("* Run '%s' with round=%s, restart_round=%s, tuning=%s, "
          "out_of_range_check=%s, omp_num_threads=%s, cpu_affinity_policy=%s, "
          "gpu_perf_hint=%s, gpu_priority_hint=%s" %
          (model_tag, running_round, restart_round, str(tuning),
           str(out_of_range_check), omp_num_threads, cpu_affinity_policy,
           gpu_perf_hint, gpu_priority_hint))
    mace_model_path = ""
    if build_type == BuildType.proto:
        mace_model_path = "%s/%s.pb" % (mace_model_dir, model_tag)
    if linkshared == 0:
        mace_run_target = "mace_run_static"
    else:
        mace_run_target = "mace_run_shared"
    if abi == "host":
        p = subprocess.Popen(
            [
                "env",
                "MACE_CPP_MIN_VLOG_LEVEL=%s" % vlog_level,
                "MACE_RUNTIME_FAILURE_RATIO=%f" % runtime_failure_ratio,
                "%s/%s" % (mace_run_dir, mace_run_target),
                "--model_name=%s" % model_tag,
                "--input_node=%s" % ",".join(input_nodes),
                "--output_node=%s" % ",".join(output_nodes),
                "--input_shape=%s" % ":".join(input_shapes),
                "--output_shape=%s" % ":".join(output_shapes),
                "--input_file=%s/%s" % (model_output_dir, input_file_name),
                "--output_file=%s/%s" % (model_output_dir, output_file_name),
                "--model_data_file=%s/%s.data" % (mace_model_dir, model_tag),
                "--device=%s" % device_type,
                "--round=%s" % running_round,
                "--restart_round=%s" % restart_round,
                "--omp_num_threads=%s" % omp_num_threads,
                "--cpu_affinity_policy=%s" % cpu_affinity_policy,
                "--gpu_perf_hint=%s" % gpu_perf_hint,
                "--gpu_priority_hint=%s" % gpu_priority_hint,
                "--model_file=%s" % mace_model_path,
            ],
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE)
        out, err = p.communicate()
        stdout = err + out
        print stdout
        print("Running finished!\n")
        return stdout
    else:
        sh.adb("-s", serialno, "shell", "mkdir", "-p", phone_data_dir)
        internal_storage_dir = create_internal_storage_dir(
            serialno, phone_data_dir)

        for input_name in input_nodes:
            formatted_name = common.formatted_file_name(input_file_name,
                                                        input_name)
            adb_push("%s/%s" % (model_output_dir, formatted_name),
                     phone_data_dir, serialno)
        if address_sanitizer:
            adb_push(find_asan_rt_library(abi), phone_data_dir, serialno)

        if not embed_model_data:
            adb_push("%s/%s.data" % (mace_model_dir, model_tag),
                     phone_data_dir, serialno)

        if device_type == common.DeviceType.GPU\
                and os.path.exists(opencl_binary_file):
            adb_push(opencl_binary_file, phone_data_dir, serialno)

        adb_push("third_party/nnlib/libhexagon_controller.so",
                 phone_data_dir, serialno)

        mace_model_phone_path = ""
        if build_type == BuildType.proto:
            mace_model_phone_path = "%s/%s.pb" % (phone_data_dir, model_tag)
            adb_push(mace_model_path,
                     mace_model_phone_path,
                     serialno)

        if linkshared == 1:
            adb_push("%s/libmace.so" % shared_library_dir, phone_data_dir,
                     serialno)
            adb_push("%s/libgnustl_shared.so" % shared_library_dir,
                     phone_data_dir,
                     serialno)

        adb_push("%s/%s" % (mace_run_dir, mace_run_target), phone_data_dir,
                 serialno)

        stdout_buff = []
        process_output = make_output_processor(stdout_buff)
        adb_cmd = [
            "LD_LIBRARY_PATH=%s" % phone_data_dir,
            "MACE_TUNING=%s" % int(tuning),
            "MACE_OUT_OF_RANGE_CHECK=%s" % int(out_of_range_check),
            "MACE_CPP_MIN_VLOG_LEVEL=%s" % vlog_level,
            "MACE_RUN_PARAMETER_PATH=%s/mace_run.config" % phone_data_dir,
            "MACE_INTERNAL_STORAGE_PATH=%s" % internal_storage_dir,
            "MACE_LIMIT_OPENCL_KERNEL_TIME=%s" % limit_opencl_kernel_time,
            "MACE_RUNTIME_FAILURE_RATIO=%f" % runtime_failure_ratio,
        ]
        if address_sanitizer:
            adb_cmd.extend([
                "LD_PRELOAD=%s/%s" % (phone_data_dir,
                                      asan_rt_library_names(abi))
            ])
        adb_cmd.extend([
            "%s/%s" % (phone_data_dir, mace_run_target),
            "--model_name=%s" % model_tag,
            "--input_node=%s" % ",".join(input_nodes),
            "--output_node=%s" % ",".join(output_nodes),
            "--input_shape=%s" % ":".join(input_shapes),
            "--output_shape=%s" % ":".join(output_shapes),
            "--input_file=%s/%s" % (phone_data_dir, input_file_name),
            "--output_file=%s/%s" % (phone_data_dir, output_file_name),
            "--model_data_file=%s/%s.data" % (phone_data_dir, model_tag),
            "--device=%s" % device_type,
            "--round=%s" % running_round,
            "--restart_round=%s" % restart_round,
            "--omp_num_threads=%s" % omp_num_threads,
            "--cpu_affinity_policy=%s" % cpu_affinity_policy,
            "--gpu_perf_hint=%s" % gpu_perf_hint,
            "--gpu_priority_hint=%s" % gpu_priority_hint,
            "--model_file=%s" % mace_model_phone_path,
            "--opencl_binary_file=%s/%s" %
            (phone_data_dir, os.path.basename(opencl_binary_file)),
        ])
        adb_cmd = ' '.join(adb_cmd)
        sh.adb(
            "-s",
            serialno,
            "shell",
            adb_cmd,
            _tty_in=True,
            _out=process_output,
            _err_to_out=True)
        stdout = "".join(stdout_buff)
        if not stdout_success(stdout):
            common.MaceLogger.error("Mace Run", "Mace run failed.")
        print("Running finished!\n")
        return stdout


def validate_model(abi,
                   serialno,
                   model_file_path,
                   weight_file_path,
                   platform,
                   device_type,
                   input_nodes,
                   output_nodes,
                   input_shapes,
                   output_shapes,
                   model_output_dir,
                   phone_data_dir,
                   caffe_env,
                   input_file_name="model_input",
                   output_file_name="model_out"):
    print("* Validate with %s" % platform)
    if abi != "host":
        for output_name in output_nodes:
            formatted_name = common.formatted_file_name(
                output_file_name, output_name)
            if os.path.exists("%s/%s" % (model_output_dir,
                                         formatted_name)):
                sh.rm("-rf", "%s/%s" % (model_output_dir, formatted_name))
            adb_pull("%s/%s" % (phone_data_dir, formatted_name),
                     model_output_dir, serialno)

    if platform == "tensorflow":
        validate(platform, model_file_path, "",
                 "%s/%s" % (model_output_dir, input_file_name),
                 "%s/%s" % (model_output_dir, output_file_name), device_type,
                 ":".join(input_shapes), ":".join(output_shapes),
                 ",".join(input_nodes), ",".join(output_nodes))
    elif platform == "caffe":
        image_name = "mace-caffe:latest"
        container_name = "mace_caffe_validator"

        if caffe_env == common.CaffeEnvType.LOCAL:
            import imp
            try:
                imp.find_module('caffe')
            except ImportError:
                logger.error('There is no caffe python module.')
            validate(platform, model_file_path, weight_file_path,
                     "%s/%s" % (model_output_dir, input_file_name),
                     "%s/%s" % (model_output_dir, output_file_name),
                     device_type,
                     ":".join(input_shapes), ":".join(output_shapes),
                     ",".join(input_nodes), ",".join(output_nodes))
        elif caffe_env == common.CaffeEnvType.DOCKER:
            docker_image_id = sh.docker("images", "-q", image_name)
            if not docker_image_id:
                print("Build caffe docker")
                sh.docker("build", "-t", image_name,
                          "third_party/caffe")

            container_id = sh.docker("ps", "-qa", "-f",
                                     "name=%s" % container_name)
            if container_id and not sh.docker("ps", "-qa", "--filter",
                                              "status=running", "-f",
                                              "name=%s" % container_name):
                sh.docker("rm", "-f", container_name)
                container_id = ""
            if not container_id:
                print("Run caffe container")
                sh.docker(
                        "run",
                        "-d",
                        "-it",
                        "--name",
                        container_name,
                        image_name,
                        "/bin/bash")

            for input_name in input_nodes:
                formatted_input_name = common.formatted_file_name(
                        input_file_name, input_name)
                sh.docker(
                        "cp",
                        "%s/%s" % (model_output_dir, formatted_input_name),
                        "%s:/mace" % container_name)

            for output_name in output_nodes:
                formatted_output_name = common.formatted_file_name(
                        output_file_name, output_name)
                sh.docker(
                        "cp",
                        "%s/%s" % (model_output_dir, formatted_output_name),
                        "%s:/mace" % container_name)
            model_file_name = os.path.basename(model_file_path)
            weight_file_name = os.path.basename(weight_file_path)
            sh.docker("cp", "tools/common.py", "%s:/mace" % container_name)
            sh.docker("cp", "tools/validate.py", "%s:/mace" % container_name)
            sh.docker("cp", model_file_path, "%s:/mace" % container_name)
            sh.docker("cp", weight_file_path, "%s:/mace" % container_name)

            sh.docker(
                "exec",
                container_name,
                "python",
                "-u",
                "/mace/validate.py",
                "--platform=caffe",
                "--model_file=/mace/%s" % model_file_name,
                "--weight_file=/mace/%s" % weight_file_name,
                "--input_file=/mace/%s" % input_file_name,
                "--mace_out_file=/mace/%s" % output_file_name,
                "--device_type=%s" % device_type,
                "--input_node=%s" % ",".join(input_nodes),
                "--output_node=%s" % ",".join(output_nodes),
                "--input_shape=%s" % ":".join(input_shapes),
                "--output_shape=%s" % ":".join(output_shapes),
                _fg=True)

    print("Validation done!\n")


def build_host_libraries(model_build_type, abi):
    bazel_build("@com_google_protobuf//:protobuf_lite", abi=abi)
    bazel_build("//mace/proto:mace_cc", abi=abi)
    bazel_build("//mace/codegen:generated_opencl", abi=abi)
    bazel_build("//mace/codegen:generated_tuning_params", abi=abi)
    bazel_build("//mace/codegen:generated_version", abi=abi)
    bazel_build("//mace/utils:utils", abi=abi)
    bazel_build("//mace/core:core", abi=abi)
    bazel_build("//mace/kernels:kernels", abi=abi)
    bazel_build("//mace/ops:ops", abi=abi)
    if model_build_type == BuildType.code:
        bazel_build(
            "//mace/codegen:generated_models",
            abi=abi)


def merge_libs(target_soc,
               serial_num,
               abi,
               project_name,
               build_output_dir,
               library_output_dir,
               model_build_type,
               hexagon_mode):
    print("* Merge mace lib")
    project_output_dir = "%s/%s" % (build_output_dir, project_name)
    hexagon_lib_file = "third_party/nnlib/libhexagon_controller.so"
    library_dir = "%s/%s" % (project_output_dir, library_output_dir)
    model_bin_dir = "%s/%s/" % (library_dir, abi)

    if not os.path.exists(model_bin_dir):
        sh.mkdir("-p", model_bin_dir)
    if hexagon_mode:
        sh.cp("-f", hexagon_lib_file, library_dir)

    # make static library
    mri_stream = ""
    if abi == "host":
        mri_stream += "create %s/libmace_%s.a\n" % \
                      (model_bin_dir, project_name)
        mri_stream += (
            "addlib "
            "bazel-bin/mace/codegen/libgenerated_opencl.pic.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/codegen/libgenerated_tuning_params.pic.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/codegen/libgenerated_version.pic.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/core/libcore.pic.lo\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/kernels/libkernels.pic.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/utils/libutils.pic.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/proto/libmace_cc.pic.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/external/com_google_protobuf/libprotobuf_lite.pic.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/ops/libops.pic.lo\n")
        if model_build_type == BuildType.code:
            mri_stream += (
                "addlib "
                "bazel-bin/mace/codegen/libgenerated_models.pic.a\n")
    else:
        if not target_soc:
            mri_stream += "create %s/libmace_%s.a\n" % \
                          (model_bin_dir, project_name)
        else:
            device_name = adb_get_device_name_by_serialno(serial_num)
            mri_stream += "create %s/libmace_%s.%s.%s.a\n" % \
                          (model_bin_dir, project_name,
                           device_name, target_soc)
        if model_build_type == BuildType.code:
            mri_stream += (
                "addlib "
                "bazel-bin/mace/codegen/libgenerated_models.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/codegen/libgenerated_opencl.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/codegen/libgenerated_tuning_params.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/codegen/libgenerated_version.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/core/libcore.lo\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/kernels/libkernels.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/utils/libutils.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/proto/libmace_cc.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/external/com_google_protobuf/libprotobuf_lite.a\n")
        mri_stream += (
            "addlib "
            "bazel-bin/mace/ops/libops.lo\n")

    mri_stream += "save\n"
    mri_stream += "end\n"

    cmd = sh.Command("%s/toolchains/" % os.environ["ANDROID_NDK_HOME"] +
                     "aarch64-linux-android-4.9/prebuilt/linux-x86_64/" +
                     "bin/aarch64-linux-android-ar")

    cmd("-M", _in=mri_stream)

    print("Libs merged!\n")


def packaging_lib(libmace_output_dir, project_name):
    print("* Package libs for %s" % project_name)
    tar_package_name = "libmace_%s.tar.gz" % project_name
    project_dir = "%s/%s" % (libmace_output_dir, project_name)
    tar_package_path = "%s/%s" % (project_dir, tar_package_name)
    if os.path.exists(tar_package_path):
        sh.rm("-rf", tar_package_path)

    print("Start packaging '%s' libs into %s" % (project_name,
                                                 tar_package_path))
    sh.tar(
        "cvzf",
        "%s" % tar_package_path,
        glob.glob("%s/*" % project_dir),
        "--exclude",
        "%s/_tmp" % project_dir,
        _fg=True)
    print("Packaging Done!\n")


def build_benchmark_model(abi,
                          model_output_dir,
                          hexagon_mode,
                          linkshared=False):
    benchmark_binary_file = "%s/benchmark_model" % model_output_dir
    if os.path.exists(benchmark_binary_file):
        sh.rm("-rf", benchmark_binary_file)

    if linkshared == 0:
        benchmark_target = "//mace/benchmark:benchmark_model"
    else:
        benchmark_target = "//mace/benchmark:benchmark_model_deps_so"
    bazel_build(benchmark_target,
                abi=abi,
                hexagon_mode=hexagon_mode)

    target_bin = "/".join(bazel_target_to_bin(benchmark_target))
    if linkshared == 0:
        sh.cp("-f", target_bin, model_output_dir)
    else:
        sh.cp("-f", target_bin, "%s/benchmark_model" % model_output_dir)


def benchmark_model(abi,
                    serialno,
                    benchmark_binary_dir,
                    vlog_level,
                    embed_model_data,
                    model_output_dir,
                    mace_model_dir,
                    input_nodes,
                    output_nodes,
                    input_shapes,
                    output_shapes,
                    model_tag,
                    device_type,
                    phone_data_dir,
                    build_type,
                    opencl_binary_file,
                    shared_library_dir,
                    omp_num_threads=-1,
                    cpu_affinity_policy=1,
                    gpu_perf_hint=3,
                    gpu_priority_hint=3,
                    input_file_name="model_input",
                    linkshared=0):
    print("* Benchmark for %s" % model_tag)

    mace_model_path = ""
    if build_type == BuildType.proto:
        mace_model_path = "%s/%s.pb" % (mace_model_dir, model_tag)
    if abi == "host":
        p = subprocess.Popen(
            [
                "env",
                "MACE_CPP_MIN_VLOG_LEVEL=%s" % vlog_level,
                "%s/benchmark_model" % benchmark_binary_dir,
                "--model_name=%s" % model_tag,
                "--input_node=%s" % ",".join(input_nodes),
                "--output_node=%s" % ",".join(output_nodes),
                "--input_shape=%s" % ":".join(input_shapes),
                "--output_shape=%s" % ":".join(output_shapes),
                "--input_file=%s/%s" % (model_output_dir, input_file_name),
                "--model_data_file=%s/%s.data" % (mace_model_dir, model_tag),
                "--device=%s" % device_type,
                "--omp_num_threads=%s" % omp_num_threads,
                "--cpu_affinity_policy=%s" % cpu_affinity_policy,
                "--gpu_perf_hint=%s" % gpu_perf_hint,
                "--gpu_priority_hint=%s" % gpu_priority_hint,
                "--model_file=%s" % mace_model_path,
            ])
        p.wait()
    else:
        sh.adb("-s", serialno, "shell", "mkdir", "-p", phone_data_dir)
        internal_storage_dir = create_internal_storage_dir(
            serialno, phone_data_dir)

        for input_name in input_nodes:
            formatted_name = common.formatted_file_name(input_file_name,
                                                        input_name)
            adb_push("%s/%s" % (model_output_dir, formatted_name),
                     phone_data_dir, serialno)
        if not embed_model_data:
            adb_push("%s/%s.data" % (mace_model_dir, model_tag),
                     phone_data_dir, serialno)
        if device_type == common.DeviceType.GPU \
                and os.path.exists(opencl_binary_file):
            adb_push(opencl_binary_file, phone_data_dir, serialno)
        mace_model_phone_path = ""
        if build_type == BuildType.proto:
            mace_model_phone_path = "%s/%s.pb" % (phone_data_dir, model_tag)
            adb_push(mace_model_path,
                     mace_model_phone_path,
                     serialno)

        if linkshared == 1:
            adb_push("%s/libmace.so" % shared_library_dir, phone_data_dir,
                     serialno)
            adb_push("%s/libgnustl_shared.so" % shared_library_dir,
                     phone_data_dir,
                     serialno)
        adb_push("%s/benchmark_model" % benchmark_binary_dir, phone_data_dir,
                 serialno)

        sh.adb(
            "-s",
            serialno,
            "shell",
            "LD_LIBRARY_PATH=%s" % phone_data_dir,
            "MACE_CPP_MIN_VLOG_LEVEL=%s" % vlog_level,
            "MACE_RUN_PARAMETER_PATH=%s/mace_run.config" %
            phone_data_dir,
            "MACE_INTERNAL_STORAGE_PATH=%s" % internal_storage_dir,
            "MACE_OPENCL_PROFILING=1",
            "%s/benchmark_model" % phone_data_dir,
            "--model_name=%s" % model_tag,
            "--input_node=%s" % ",".join(input_nodes),
            "--output_node=%s" % ",".join(output_nodes),
            "--input_shape=%s" % ":".join(input_shapes),
            "--output_shape=%s" % ":".join(output_shapes),
            "--input_file=%s/%s" % (phone_data_dir, input_file_name),
            "--model_data_file=%s/%s.data" % (phone_data_dir, model_tag),
            "--device=%s" % device_type,
            "--omp_num_threads=%s" % omp_num_threads,
            "--cpu_affinity_policy=%s" % cpu_affinity_policy,
            "--gpu_perf_hint=%s" % gpu_perf_hint,
            "--gpu_priority_hint=%s" % gpu_priority_hint,
            "--model_file=%s" % mace_model_phone_path,
            "--opencl_binary_file=%s/%s" %
            (phone_data_dir, os.path.basename(opencl_binary_file)),
            _fg=True)

    print("Benchmark done!\n")


def build_run_throughput_test(abi,
                              serialno,
                              vlog_level,
                              run_seconds,
                              merged_lib_file,
                              model_input_dir,
                              embed_model_data,
                              input_nodes,
                              output_nodes,
                              input_shapes,
                              output_shapes,
                              cpu_model_tag,
                              gpu_model_tag,
                              dsp_model_tag,
                              phone_data_dir,
                              strip="always",
                              input_file_name="model_input"):
    print("* Build and run throughput_test")

    model_tag_build_flag = ""
    if cpu_model_tag:
        model_tag_build_flag += "--copt=-DMACE_CPU_MODEL_TAG=%s " % \
                                cpu_model_tag
    if gpu_model_tag:
        model_tag_build_flag += "--copt=-DMACE_GPU_MODEL_TAG=%s " % \
                                gpu_model_tag
    if dsp_model_tag:
        model_tag_build_flag += "--copt=-DMACE_DSP_MODEL_TAG=%s " % \
                                dsp_model_tag

    sh.cp("-f", merged_lib_file, "mace/benchmark/libmace_merged.a")
    sh.bazel(
        "build",
        "-c",
        "opt",
        "--strip",
        strip,
        "--verbose_failures",
        "//mace/benchmark:model_throughput_test",
        "--crosstool_top=//external:android/crosstool",
        "--host_crosstool_top=@bazel_tools//tools/cpp:toolchain",
        "--cpu=%s" % abi,
        "--copt=-std=c++11",
        "--copt=-D_GLIBCXX_USE_C99_MATH_TR1",
        "--copt=-Werror=return-type",
        "--copt=-O3",
        "--define",
        "neon=true",
        "--define",
        "openmp=true",
        model_tag_build_flag,
        _fg=True)

    sh.rm("mace/benchmark/libmace_merged.a")
    sh.adb("-s",
           serialno,
           "shell",
           "mkdir",
           "-p",
           phone_data_dir)
    adb_push("%s/%s_%s" % (model_input_dir, input_file_name,
                           ",".join(input_nodes)),
             phone_data_dir,
             serialno)
    adb_push("bazel-bin/mace/benchmark/model_throughput_test",
             phone_data_dir,
             serialno)
    if not embed_model_data:
        adb_push("codegen/models/%s/%s.data" % cpu_model_tag,
                 phone_data_dir,
                 serialno)
        adb_push("codegen/models/%s/%s.data" % gpu_model_tag,
                 phone_data_dir,
                 serialno)
        adb_push("codegen/models/%s/%s.data" % dsp_model_tag,
                 phone_data_dir,
                 serialno)
    adb_push("third_party/nnlib/libhexagon_controller.so",
             phone_data_dir,
             serialno)

    sh.adb(
        "-s",
        serialno,
        "shell",
        "LD_LIBRARY_PATH=%s" % phone_data_dir,
        "MACE_CPP_MIN_VLOG_LEVEL=%s" % vlog_level,
        "MACE_RUN_PARAMETER_PATH=%s/mace_run.config" %
        phone_data_dir,
        "%s/model_throughput_test" % phone_data_dir,
        "--input_node=%s" % ",".join(input_nodes),
        "--output_node=%s" % ",".join(output_nodes),
        "--input_shape=%s" % ":".join(input_shapes),
        "--output_shape=%s" % ":".join(output_shapes),
        "--input_file=%s/%s" % (phone_data_dir, input_file_name),
        "--cpu_model_data_file=%s/%s.data" % (phone_data_dir,
                                              cpu_model_tag),
        "--gpu_model_data_file=%s/%s.data" % (phone_data_dir,
                                              gpu_model_tag),
        "--dsp_model_data_file=%s/%s.data" % (phone_data_dir,
                                              dsp_model_tag),
        "--run_seconds=%s" % run_seconds,
        _fg=True)

    print("throughput_test done!\n")


################################
# falcon
################################
def falcon_tags(tags_dict):
    tags = ""
    for k, v in tags_dict.iteritems():
        if tags == "":
            tags = "%s=%s" % (k, v)
        else:
            tags = tags + ",%s=%s" % (k, v)
    return tags


def falcon_push_metrics(server, metrics, endpoint="mace_dev", tags={}):
    cli = falcon_cli.FalconCli.connect(server=server, port=8433, debug=False)
    ts = int(time.time())
    falcon_metrics = [{
        "endpoint": endpoint,
        "metric": key,
        "tags": falcon_tags(tags),
        "timestamp": ts,
        "value": value,
        "step": 600,
        "counterType": "GAUGE"
    } for key, value in metrics.iteritems()]
    cli.update(falcon_metrics)
