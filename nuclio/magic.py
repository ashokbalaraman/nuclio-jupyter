# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import re
import shlex
from argparse import ArgumentParser
from glob import glob
from os import environ, path
from shutil import unpack_archive
from subprocess import run
from sys import executable, stderr
from tempfile import mkdtemp
from urllib.parse import urlencode, urljoin
from urllib.request import urlopen

import ipykernel
from IPython import get_ipython
from IPython.core.magic import register_line_cell_magic
from notebook.notebookapp import list_running_servers

log_prefix = '%nuclio: '

# Make sure we're working when not running under IPython/Jupyter
kernel = get_ipython()
if kernel is None:
    def register_line_cell_magic(fn):  # noqa
        return fn

# name -> function
commands = {}


def noop_log(msg):
    pass


def verbose_log(message):
    print('{}{}'.format(log_prefix, message))


log = verbose_log


def log_error(msg):
    print('{}{}'.format(log_prefix, msg), file=stderr)


def command(fn):
    """Decorator to register fn as nuclio magic command"""
    commands[fn.__name__] = fn
    return fn


@register_line_cell_magic
def nuclio(line, cell=None):
    line = line.strip()
    if not line:
        log_error('require one of: {}'.format(sorted(commands)))
        return

    cmd = line.split()[0].lower()
    fn = commands.get(cmd)
    if fn is None:
        log_error('unknown command: {}'.format(cmd))
        return

    line = line[len(cmd):].strip()  # Remove command from line
    fn(line, cell)


@command
def verbose(line, cell):
    """Toggle verbose mode.

    Example:
    In [1]: %nuclio verobose
    %nuclio: verbose off
    In [2]: %nuclio verobose
    %nuclio: verbose on
    """
    global log

    log = noop_log if log is verbose_log else verbose_log
    print('%nuclio: verbose {}'.format('on' if log is verbose_log else 'off'))


def parse_env(line):
    i = line.find('=')
    if i == -1:
        return None, None
    key, value = line[:i].strip(), line[i+1:].strip()
    return key, value


def set_env(line):
    key, value = parse_env(line)
    if key is None:
        log_error('cannot find "=" in line')
        return
    # We don't print the value since it might be password, API key ...
    log('setting {!r} environment variable'.format(key))
    environ[key] = value


def cell_lines(cell):
    if cell is None:
        return []

    return filter(str.strip, cell.splitlines())


@command
def env(line, cell):
    """Set environment variable. Will update "spec.env" in configuration.

    Examples:
    In [1]: %nuclio env USER=iguzaio
    %nuclio: setting 'iguazio' environment variable

    In [2]: %%nuclio env
    ...: USER=iguazio
    ...: PASSWORD=t0ps3cr3t
    ...:
    ...:
    %nuclio: setting 'USER' environment variable
    %nuclio: setting 'PASSWORD' environment variable
    """
    if line:
        set_env(line)

    for line in cell_lines(cell):
        set_env(line)


@command
def help(line, cell):
    """Print help on command.

    Example:
    In [1]: %nuclio help
    Available commands:
    - env
    - env_file
    ...

    In [2]: %nuclio help env
    ... (verbose env)
    """
    cmd = line.strip().lower()
    if not cmd:
        print('Show help on command. Available commands:')
        for cmd, fn in sorted(commands.items()):
            doc = fn.__doc__
            if doc is None:
                short_help = ''
            else:
                i = doc.find('.')
                short_help = doc[:i] if i != -1 else doc[:40]
            print('    - {}: {}'.format(cmd, short_help))
        return

    fn = commands.get(cmd)
    if not fn:
        log_error('unknown command: {}'.format(cmd))
        return

    print(fn.__doc__)


def iter_env_lines(fp):
    for line in fp:
        line = line.strip()
        if not line or line[0] == '#':
            continue
        yield line


def env_from_file(path):
    with open(path) as fp:
        for line in iter_env_lines(fp):
            set_env(line)


@command
def env_file(line, cell):
    """Set environment from file(s). Will update "spec.env" in configuration.

    Examples:
    In [1]: %nuclio env_file env.yml

    In [2]: %%nuclio env_file
    ...: env.yml
    ...: dev-env.yml
    """
    if line:
        env_from_file(line.strip())

    for line in cell_lines(cell):
        env_from_file(line)


@command
def cmd(line, cell):
    """Run a command, add it to "build.Commands" in exported configuration.

    Examples:
    In [1]: %nuclio cmd pip install chardet==1.0.1

    In [2]: %%nuclio cmd
    ...: apt-get install -y libyaml-dev
    ...: pip install pyyaml==3.13

    If you'd like to only to add the instructions to function.yaml without
    running it locally, use the '--config-only' or '-c' flag

    In [3]: %nuclio cmd --config-only apt-get install -y libyaml-dev
    """
    if line.startswith('--config-only') or line.startswith('-c'):
        return

    ipy = get_ipython()
    if line:
        ipy.system(line)

    for line in cell_lines(cell):
        ipy.system(line)


# TODO
# @command
def deploy(line, cell):
    """Deploy function .

    Examples:
    In [1]: %nuclio deploy
    %nuclio: function deployed

    In [2] %nuclio deploy http://localhost:8080
    %nuclio: function deployed
    """
    # TODO: Deploy parameters
    # - dashboard URL
    # - project name
    # - function name
    print('TBD ☺')


@command
def handler(line, cell):
    """Mark this cell as handler function.

    %%nuclio handler
    ctx.logger.info('handler called')
    # nuclio:return
    'Hello ' + event.body

    Will become

    def handler(context, event):
        ctx.logger.info('handler called')
        # nuclio:return
        return 'Hello ' + event.body
    """
    kernel.run_cell(cell)


# Based on
# https://github.com/jupyter/notebook/issues/1000#issuecomment-359875246
def notebook_file_name():
    """Return the full path of the jupyter notebook."""
    kernel_id = re.search('kernel-(.*).json',
                          ipykernel.connect.get_connection_file()).group(1)
    servers = list_running_servers()
    for srv in servers:
        query = {'token': srv.get('token', '')}
        url = urljoin(srv['url'], 'api/sessions') + '?' + urlencode(query)
        for session in json.load(urlopen(url)):
            if session['kernel']['id'] == kernel_id:
                relative_path = session['notebook']['path']
                return path.join(srv['notebook_dir'], relative_path)


@command
def export(line, cell, return_dir=False):
    """Export notebook.

    Example:
    In [1] %nuclio export
    Notebook exported to handler at '/tmp/nuclio-handler-99'
    In [2] %nuclio export --output-dir /tmp/handler
    Notebook exported to handler at '/tmp/handler'
    In [3] %nuclio export --notebook /path/to/notebook.ipynb
    Notebook exported to handler at '/tmp/nuclio-handler-29803'
    """

    args, rest = parse_export_line(line)
    if rest:
        log_error('nunknown arguments: {}'.format(' '.join(rest)))
        return

    notebook = args.notebook or notebook_file_name()
    if not notebook:
        log_error('cannot find notebook name (try with --notebook)')
        return

    out_dir = args.output_dir or mkdtemp(prefix='nuclio-handler-')

    cmd = [
        executable, '-m', 'nbconvert',
        '--to', 'nuclio.export.NuclioExporter',
        '--output-dir', out_dir,
        notebook,
    ]
    out = run(cmd, capture_output=True)
    if out.returncode != 0:
        print(out.stdout.decode('utf-8'))
        print(out.stderr.decode('utf-8'), file=stderr)
        log_error('cannot convert notebook')
        return

    out_files = glob('{}/*.zip'.format(out_dir))
    if not out_files:
        log_error('cannot find zip files in {}'.format(out_dir))
        return

    unpack_archive(out_files[0], out_dir)
    log('handler exported to {}'.format(out_dir))
    if return_dir:
        return out_dir


def parse_export_line(line):
    parser = ArgumentParser(prog='%nuclio')
    parser.add_argument('--output-dir')
    parser.add_argument('--notebook')

    return parser.parse_known_args(shlex.split(line))


def print_on_of(pattern):
    files = glob(pattern)
    if not files:
        raise ValueError('no match for {}'.format(pattern))

    fname = files[0]
    print('--- {} ---\n'.format(path.basename(fname)))
    with open(fname) as fp:
        print(fp.read())


def print_handler_code(notebook_file=None):
    """Prints handler code (as it was exported).

   You should save the notebook before calling this function.
    """
    notebook_file = notebook_file or notebook_file_name()
    if not notebook_file:
        raise ValueError('cannot find notebook file name')

    line = '--notebook {}'.format(notebook_file)
    out_dir = export(line, None, return_dir=True)
    if not out_dir:
        raise ValueError('failed to export {}'.format(notebook_file))

    print_on_of('{}/*.py'.format(out_dir))
    print_on_of('{}/*.yaml'.format(out_dir))
