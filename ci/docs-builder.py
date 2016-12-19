#!/usr/bin/env python

import argparse
import subprocess
import sys
import os
from shutil import copyfile

import yaml

from lib import builder
from lib.builder import WORKING_DIR


LATEST = '2.11'

USERNAME = '57600fb10c1e664383000229'
HOSTNAME = 'docs-pulp.rhcloud.com'

SITE_ROOT = '~/app-root/repo/diy/'

# dict of {git repo: [list of package dirs containing setup.py]} that need to be installed
# for apidoc generation to work; only used for pulp 3+
APIDOC_PACKAGES = {
    'pulp': ['app', 'common', 'exceptions', 'plugin', 'tasking']
}


def get_components(configuration):
    # Get the components from the yaml file
    repos = configuration['repositories']
    for component in repos:
        yield component


def load_config(config_name):
    # Get the config
    config_file = os.path.join(os.path.dirname(__file__),
                               'config', 'releases', '%s.yaml' % config_name)
    if not os.path.exists(config_file):
        print("Error: %s not found. " % config_file)
        sys.exit(1)
    with open(config_file, 'r') as config_handle:
        config = yaml.safe_load(config_handle)
    return config


def update_version(config_name):
    script_dir = os.path.abspath(os.path.dirname(__file__))
    # script does not push to github, but it does check out all the repos we want and
    # makes sure their version reflect the versions in the build config being used
    script = os.path.join(script_dir, 'update-version-and-merge-forward.py')
    subprocess.check_call([script, config_name])


def main():
    # Parse the args
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, help="Build the docs for a given release.")
    opts = parser.parse_args()
    is_pulp3 = opts.release.startswith('3')

    configuration = load_config(opts.release)

    # Get platform build version
    repo_list = configuration['repositories']
    try:
        pulp_dict = list(filter(lambda x: x['name'] == 'pulp', repo_list))[0]
    except IndexError:
        raise RuntimeError("config file does not have an entry for 'pulp'")
    version = pulp_dict['version']

    if version.endswith('alpha'):
        build_type = 'nightly'
    elif version.endswith('beta'):
        build_type = 'testing'
    elif version.endswith('rc'):
        build_type = 'testing'
    else:
        build_type = 'ga'

    x_y_version = '.'.join(version.split('.')[:2])

    builder.ensure_dir(WORKING_DIR, clean=True)

    # use the version update scripts to check out git repos and ensure correct versions
    update_version(opts.release)

    # install any apidoc dependencies that exist for pulp 3 docs
    if is_pulp3:
        for repo, packages in APIDOC_PACKAGES.items():
            for package in packages:
                package_dir = os.path.join(WORKING_DIR, repo, package)
                if os.path.exists(package_dir):
                    subprocess.check_call(['python', 'setup.py', 'develop'], cwd=package_dir)

    plugins_dir = os.sep.join([WORKING_DIR, 'pulp', 'docs', 'plugins'])
    builder.ensure_dir(plugins_dir)

    for component in get_components(configuration):
        if component['name'] == 'pulp':
            continue

        src = os.sep.join([WORKING_DIR, component['name'], 'docs'])
        dst = os.sep.join([plugins_dir, component['name']])
        os.symlink(src, dst)

    # copy in the pulp_index.rst file
    if is_pulp3:
        src_path = 'docs/pulp_index_pulp3.rst'
    else:
        src_path = 'docs/pulp_index.rst'
    pulp_index_rst = os.sep.join([WORKING_DIR, 'pulp', 'docs', 'index.rst'])
    copyfile(src_path, pulp_index_rst)

    # copy in the plugin_index.rst file
    plugin_index_rst = os.sep.join([plugins_dir, 'index.rst'])
    copyfile('docs/plugin_index.rst', plugin_index_rst)

    # copy in the all_content_index.rst file
    all_content_index_rst = os.sep.join([WORKING_DIR, 'pulp', 'docs', 'all_content_index.rst'])
    if is_pulp3:
        copyfile('docs/all_content_index_pulp3.rst', all_content_index_rst)
    else:
        copyfile('docs/all_content_index.rst', all_content_index_rst)

    # make the _templates dir
    layout_dir = os.sep.join([WORKING_DIR, 'pulp', 'docs', '_templates'])
    os.makedirs(layout_dir)

    # copy in the layout.html file for analytics
    layout_html_path = os.sep.join([WORKING_DIR, 'pulp', 'docs', '_templates', 'layout.html'])
    copyfile('docs/layout.html', layout_html_path)

    # build the docs via the Pulp project itself
    print("Building the docs")
    docs_directory = os.sep.join([WORKING_DIR, 'pulp', 'docs'])
    make_command = ['make', 'html']
    exit_code = subprocess.call(make_command, cwd=docs_directory)
    if exit_code != 0:
        raise RuntimeError('An error occurred while building the docs.')

    # rsync the docs to the root if it's GA of latest
    if build_type == 'ga' and x_y_version == LATEST:
        local_path_arg = os.sep.join([docs_directory, '_build', 'html']) + os.sep
        remote_path_arg = '%s@%s:%s' % (USERNAME, HOSTNAME, SITE_ROOT)
        rsync_command = ['rsync', '-avzh', '--delete', '--exclude', 'en',
                         local_path_arg, remote_path_arg]
        exit_code = subprocess.call(rsync_command, cwd=docs_directory)
        if exit_code != 0:
            raise RuntimeError('An error occurred while pushing latest docs to OpenShift.')

    # rsync the nightly "master" docs to an unversioned "nightly" dir for
    # easy linking to in-development docs: /en/nightly/
    if build_type == 'nightly' and opts.release == 'master':
        local_path_arg = os.sep.join([docs_directory, '_build', 'html']) + os.sep
        remote_path_arg = '%s@%s:%sen/%s/' % (USERNAME, HOSTNAME, SITE_ROOT, build_type)
        path_option_arg = 'mkdir -p %sen/%s/ && rsync' % (SITE_ROOT, build_type)
        rsync_command = ['rsync', '-avzh', '--rsync-path', path_option_arg, '--delete',
                         local_path_arg, remote_path_arg]
        exit_code = subprocess.call(rsync_command, cwd=docs_directory)
        if exit_code != 0:
            raise RuntimeError('An error occurred while pushing nightly docs to OpenShift.')

    # rsync the docs to OpenShift
    local_path_arg = os.sep.join([docs_directory, '_build', 'html']) + os.sep
    remote_path_arg = '%s@%s:%sen/%s/' % (USERNAME, HOSTNAME, SITE_ROOT, x_y_version)
    if build_type != 'ga':
        remote_path_arg += build_type + '/'
        path_option_arg = 'mkdir -p %sen/%s/%s/ && rsync' % (SITE_ROOT, x_y_version, build_type)
        rsync_command = ['rsync', '-avzh', '--rsync-path', path_option_arg, '--delete',
                         local_path_arg, remote_path_arg]
    else:
        path_option_arg = 'mkdir -p %sen/%s/ && rsync' % (SITE_ROOT, x_y_version)
        rsync_command = ['rsync', '-avzh', '--rsync-path', path_option_arg, '--delete',
                         '--exclude', 'nightly', '--exclude', 'testing',
                         local_path_arg, remote_path_arg]
    exit_code = subprocess.call(rsync_command, cwd=docs_directory)
    if exit_code != 0:
        raise RuntimeError('An error occurred while pushing docs to OpenShift.')

    # rsync the robots.txt to OpenShift
    local_path_arg = 'docs/robots.txt'
    remote_path_arg = '%s@%s:%s' % (USERNAME, HOSTNAME, SITE_ROOT)
    scp_command = ['scp', local_path_arg, remote_path_arg]
    exit_code = subprocess.call(scp_command)
    if exit_code != 0:
        raise RuntimeError('An error occurred while pushing robots.txt to OpenShift.')

    # rsync the testrubyserver.rb to OpenShift
    local_path_arg = 'docs/testrubyserver.rb'
    remote_path_arg = '%s@%s:%s' % (USERNAME, HOSTNAME, SITE_ROOT)
    scp_command = ['scp', local_path_arg, remote_path_arg]
    exit_code = subprocess.call(scp_command)
    if exit_code != 0:
        raise RuntimeError('An error occurred while pushing testrubyserver.rb to OpenShift.')

    # add symlink for latest
    symlink_cmd = [
        'ssh',
        '%s@%s' % (USERNAME, HOSTNAME),
        'ln -sfn %sen/%s %sen/latest' % (SITE_ROOT, LATEST, SITE_ROOT)
    ]
    exit_code = subprocess.call(symlink_cmd)
    if exit_code != 0:
        raise RuntimeError("An error occurred while creating the 'latest' symlink "
                           "testrubyserver.rb to OpenShift.")

if __name__ == "__main__":
    main()
