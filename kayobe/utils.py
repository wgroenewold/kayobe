# Copyright (c) 2017 StackHPC Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import base64
import glob
import logging
import os
import shutil
import subprocess
import sys

import yaml

from kayobe import exception


LOG = logging.getLogger(__name__)


def get_data_files_path(*relative_path):
    """Given a relative path to a data file, return the absolute path"""
    # Detect editable pip install / python setup.py develop and use a path
    # relative to the source directory
    return os.path.join(_get_base_path(), *relative_path)


def _detect_install_prefix(path):
    script_path = os.path.realpath(path)
    script_path = os.path.normpath(script_path)
    components = script_path.split(os.sep)
    # use heuristic: anything before the last 'lib' in path is the prefix
    if 'lib' not in components:
        return None
    last_lib = len(components) - 1 - components[::-1].index('lib')
    prefix = components[:last_lib]
    prefix_path = os.sep.join(prefix)
    return prefix_path


def _get_base_path():
    override = os.environ.get("KAYOBE_DATA_FILES_PATH")
    if override:
        return os.path.join(override)
    egg_glob = os.path.join(
        sys.prefix, 'lib*', 'python*', '*-packages', 'kayobe.egg-link'
    )
    egg_link = glob.glob(egg_glob)
    if egg_link:
        with open(egg_link[0], "r") as f:
            realpath = f.readline().strip()
        return os.path.join(realpath)

    prefix = _detect_install_prefix(__file__)
    if prefix:
        return os.path.join(prefix, "share", "kayobe")

    # Assume uninstalled
    return os.path.join(os.path.realpath(__file__), "..")


def galaxy_role_install(role_file, roles_path, force=False):
    """Install Ansible roles via Ansible Galaxy."""
    cmd = ["ansible-galaxy", "role", "install"]
    cmd += ["--roles-path", roles_path]
    cmd += ["--role-file", role_file]
    if force:
        cmd += ["--force"]
    try:
        run_command(cmd)
    except subprocess.CalledProcessError as e:
        LOG.error("Failed to install Ansible roles from %s via Ansible "
                  "Galaxy: returncode %d", role_file, e.returncode)
        sys.exit(e.returncode)


def galaxy_collection_install(requirements_file, collections_path,
                              force=False):
    requirements = read_yaml_file(requirements_file)
    if not isinstance(requirements, dict):
        # Handle legacy role list format, which causes the command to fail.
        return
    cmd = ["ansible-galaxy", "collection", "install"]
    cmd += ["--collections-path", collections_path]
    cmd += ["--requirements-file", requirements_file]
    if force:
        cmd += ["--force"]
    try:
        run_command(cmd)
    except subprocess.CalledProcessError as e:
        LOG.error("Failed to install Ansible collections from %s via Ansible "
                  "Galaxy: returncode %d", requirements_file, e.returncode)
        sys.exit(e.returncode)


def galaxy_remove(roles_to_remove, roles_path):

    """Remove Ansible roles via Ansible Galaxy."""
    cmd = ["ansible-galaxy", "role", "remove"]
    cmd += ["--roles-path", roles_path]
    cmd += roles_to_remove
    try:
        run_command(cmd)
    except subprocess.CalledProcessError as e:
        LOG.error("Failed to remove Ansible roles %s via Ansible "
                  "Galaxy: returncode %d",
                  ",".join(roles_to_remove), e.returncode)
        sys.exit(e.returncode)


def read_file(path, mode="r"):
    """Read the content of a file."""
    with open(path, mode) as f:
        return f.read()


def read_yaml_file(path):
    """Read and decode a YAML file."""
    try:
        content = read_file(path)
    except IOError as e:
        print("Failed to open config dump file %s: %s" %
              (path, repr(e)))
        sys.exit(1)
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as e:
        print("Failed to decode config dump YAML file %s: %s" %
              (path, repr(e)))
        sys.exit(1)


def is_readable_dir(path):
    """Check whether a path references a readable directory."""
    if not os.path.exists(path):
        return {"result": False, "message": "Path does not exist"}
    if not os.path.isdir(path):
        return {"result": False, "message": "Path is not a directory"}
    if not os.access(path, os.R_OK):
        return {"result": False, "message": "Directory is not readable"}
    return {"result": True}


def is_readable_file(path):
    """Check whether a path references a readable file."""
    if not os.path.exists(path):
        return {"result": False, "message": "Path does not exist"}
    if not os.path.isfile(path):
        return {"result": False, "message": "Path is not a file"}
    if not os.access(path, os.R_OK):
        return {"result": False, "message": "File is not readable"}
    return {"result": True}


def run_command(cmd, quiet=False, check_output=False, **kwargs):
    """Run a command, checking the output.

    :param quiet: Redirect output to /dev/null
    :param check_output: Whether to return the output of the command
    :returns: The output of the command if check_output is true
    """
    if isinstance(cmd, str):
        cmd_string = cmd
    else:
        cmd_string = " ".join(cmd)
    LOG.debug("Running command: %s", cmd_string)
    if quiet:
        with open("/dev/null", "w") as devnull:
            kwargs["stdout"] = devnull
            kwargs["stderr"] = devnull
            subprocess.check_call(cmd, **kwargs)
    elif check_output:
        return subprocess.check_output(cmd, **kwargs)
    else:
        subprocess.check_call(cmd, **kwargs)


def quote_and_escape(value):
    """Quote and escape a string.

    Adds enclosing single quotes to the string passed, and escapes single
    quotes within the string using backslashes. This is useful for passing
    'extra vars' to Ansible. Without this, Ansible only uses the part of the
    string up to the first whitespace.

    :param value: the string to quote and escape.
    :returns: the quoted and escaped string.
    """
    if not isinstance(value, str):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def escape_jinja(string):
    """Escapes a string so that jinja template variables are not expanded

    :param string: the string to escape
    :return: the escaped string
    """
    # We base64 encode the string to avoid the need to escape characters.
    # This is because ansible has some parsing quirks that makes it fairly
    # hard to escape stuff in generic way.
    # See: https://github.com/ansible/ansible/issues/10464

    b64_value = base64.b64encode(string.encode())
    return ''.join(('{{', "'", b64_value.decode(), "' | b64decode ", '}}'))


def intersect_limits(args_limit, cli_limit):
    """Create an Ansible host pattern of the intersection of two patterns.

    :param args_limit: user-specified limit, or None.
    :param cli_limit: limit originating from this CLI, or None.
    :returns: a string representing an intersection of the two patterns.
    """
    # NOTE(mgoddard): Ansible uses either commas (,) or colons (:) to separate
    # parts of a host pattern. An intersection is specified using a separator
    # followed by an ampersand (&). If a mix of comma and colon separators is
    # used, Ansible picks one and treats the other as part of the host pattern.
    # This leads to hard to diagnose errors. Try to determine which separator
    # the user has specified, and be consistent. Error if both are used.
    if args_limit and ',' in args_limit:
        if ':' in args_limit:
            raise exception.Error("Invalid format for host limit argument. "
                                  "Cannot mix commas and colons to separate "
                                  "hosts")
        separator = ',&'
    else:
        separator = ':&'
    limits = [l for l in [args_limit, cli_limit] if l]
    return separator.join(limits)


def copy_dir(src, dest, exclude=None):
    """Copy recursively a directory.

    :param src: path of the source directory
    :param dest: destination path, will be created if it does not exist
    :param exclude: names of files or directories at the root of the source
                    directory to exclude during copy
    """
    if exclude is None:
        exclude = []

    if not os.path.isdir(dest):
        os.mkdir(dest)

    for file in os.listdir(src):
        if file in exclude:
            continue

        src_path = os.path.join(src, file)
        dest_path = os.path.join(dest, file)
        if os.path.isdir(src_path):
            copy_dir(src_path, dest_path)
        else:
            shutil.copy2(src_path, dest_path)
