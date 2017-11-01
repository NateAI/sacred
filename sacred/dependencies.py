#!/usr/bin/env python
# coding=utf-8
from __future__ import division, print_function, unicode_literals

import functools
import hashlib
import os.path
import re
import sys

import pkg_resources
from pip.commands.show import search_packages_info

import sacred.optional as opt
from sacred import SETTINGS
from sacred.utils import is_subdir, iter_prefixes

__sacred__ = True  # marks files that should be filtered from stack traces

MB = 1048576
MODULE_BLACKLIST = {None, '__future__', '__main__', 'hashlib', 'os', 're'} | \
    set(sys.builtin_module_names)
module = type(sys)
PEP440_VERSION_PATTERN = re.compile(r"""
^
(\d+!)?              # epoch
(\d[.\d]*(?<= \d))   # release
((?:[abc]|rc)\d+)?   # pre-release
(?:(\.post\d+))?     # post-release
(?:(\.dev\d+))?      # development release
$
""", flags=re.VERBOSE)


def get_py_file_if_possible(pyc_name):
    """Try to retrieve a X.py file for a given X.py[c] file."""
    if pyc_name.endswith('.py'):
        return pyc_name
    assert pyc_name.endswith('.pyc')
    non_compiled_file = pyc_name[:-1]
    if os.path.exists(non_compiled_file):
        return non_compiled_file
    return pyc_name


def get_digest(filename):
    """Compute the MD5 hash for a given file."""
    h = hashlib.md5()
    with open(filename, 'rb') as f:
        data = f.read(1 * MB)
        while data:
            h.update(data)
            data = f.read(1 * MB)
        return h.hexdigest()


def get_commit_if_possible(filename):
    """Try to retrieve VCS information for a given file.

    Currently only supports git using the gitpython package.

    Parameters
    ----------
    filename : str

    Returns
    -------
        path: str
            The base path of the repository
        commit: str
            The commit hash
        is_dirty: bool
            True if there are uncommitted changes in the repository
    """
    # git
    if opt.has_gitpython:
        from git import Repo, InvalidGitRepositoryError
        try:
            directory = os.path.dirname(filename)
            repo = Repo(directory, search_parent_directories=True)
            try:
                path = repo.remote().url
            except ValueError:
                path = 'git:/' + repo.working_dir
            is_dirty = repo.is_dirty()
            commit = repo.head.commit.hexsha
            return path, commit, is_dirty
        except InvalidGitRepositoryError:
            pass
    return None, None, None


@functools.total_ordering
class Source(object):
    def __init__(self, filename, digest, repo, commit, isdirty):
        self.filename = filename
        self.digest = digest
        self.repo = repo
        self.commit = commit
        self.is_dirty = isdirty

    @staticmethod
    def create(filename):
        if not filename or not os.path.exists(filename):
            raise ValueError('invalid filename or file not found "{}"'
                             .format(filename))

        main_file = get_py_file_if_possible(os.path.abspath(filename))
        repo, commit, is_dirty = get_commit_if_possible(main_file)
        return Source(main_file, get_digest(main_file), repo, commit, is_dirty)

    def to_json(self, base_dir=None):
        if base_dir:
            return os.path.relpath(self.filename, base_dir), self.digest
        else:
            return self.filename, self.digest

    def __hash__(self):
        return hash(self.filename)

    def __eq__(self, other):
        if isinstance(other, Source):
            return self.filename == other.filename
        elif isinstance(other, opt.basestring):
            return self.filename == other
        else:
            return False

    def __le__(self, other):
        return self.filename.__le__(other.filename)

    def __repr__(self):
        return '<Source: {}>'.format(self.filename)


@functools.total_ordering
class PackageDependency(object):
    def __init__(self, name, version):
        self.name = name
        self.version = version

    def fill_missing_version(self):
        if self.version is not None:
            return
        dist = pkg_resources.working_set.by_key.get(self.name)
        self.version = dist.version if dist else '<unknown>'

    def to_json(self):
        return '{}=={}'.format(self.name, self.version)

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        if isinstance(other, PackageDependency):
            return self.name == other.name
        else:
            return False

    def __le__(self, other):
        return self.name.__le__(other.name)

    def __repr__(self):
        return '<PackageDependency: {}={}>'.format(self.name, self.version)

    @staticmethod
    def get_version_heuristic(mod):
        possible_version_attributes = ['__version__', 'VERSION', 'version']
        for vattr in possible_version_attributes:
            if hasattr(mod, vattr):
                version = getattr(mod, vattr)
                if isinstance(version, opt.basestring) and \
                        PEP440_VERSION_PATTERN.match(version):
                    return version
                if isinstance(version, tuple):
                    version = '.'.join([str(n) for n in version])
                    if PEP440_VERSION_PATTERN.match(version):
                        return version

        return None

    @staticmethod
    def create(mod):
        modname = mod.__name__
        version = PackageDependency.get_version_heuristic(mod)
        return PackageDependency(modname, version)


def splitall(path):
    """Split a path into a list of directory names (and optionally a filename).

    Parameters
    ----------
    path: str
        The path (absolute or relative).

    Returns
    -------
    allparts: list[str]
        List of directory names (and optionally a filename)

    Example
    -------
    "foo/bar/baz.py" => ["foo", "bar", "baz.py"]
    "/absolute/path.py" => ["/", "absolute", "baz.py"]

    Notes
    -----
    Credit to Trent Mick. Taken from
    https://www.safaribooksonline.com/library/view/python-cookbook/0596001673/ch04s16.html
    """
    allparts = []
    while True:
        parts = os.path.split(path)
        if parts[0] == path:  # sentinel for absolute paths
            allparts.insert(0, parts[0])
            break
        elif parts[1] == path:  # sentinel for relative paths
            allparts.insert(0, parts[1])
            break
        else:
            path = parts[0]
            allparts.insert(0, parts[1])
    return allparts


def convert_path_to_module_parts(path):
    """Convert path to a python file into list of module names."""
    module_parts = splitall(path)
    if module_parts[-1] in ['__init__.py', '__init__.pyc']:
        # remove trailing __init__.py
        module_parts = module_parts[:-1]
    else:
        # remove file extension
        module_parts[-1], _ = os.path.splitext(module_parts[-1])
    return module_parts


def is_local_source(filename, modname, experiment_path):
    """Check if a module comes from the given experiment path.

    Check if a module, given by name and filename, is from (a subdirectory of )
    the given experiment path.
    This is used to determine if the module is a local source file, or rather
    a package dependency.

    Parameters
    ----------
    filename: str
        The absolute filename of the module in question.
        (Usually module.__file__)
    modname: str
        The full name of the module including parent namespaces.
    experiment_path: str
        The base path of the experiment.

    Returns
    -------
    bool:
        True if the module was imported locally from (a subdir of) the
        experiment_path, and False otherwise.
    """
    if not is_subdir(filename, experiment_path):
        return False
    rel_path = os.path.relpath(filename, experiment_path)
    path_parts = convert_path_to_module_parts(rel_path)

    mod_parts = modname.split('.')
    if path_parts == mod_parts:
        return True
    if len(path_parts) > len(mod_parts):
        return False
    abs_path_parts = convert_path_to_module_parts(os.path.abspath(filename))
    return all([p == m for p, m in zip(reversed(abs_path_parts),
                                       reversed(mod_parts))])


def get_main_file(globs):
    filename = globs.get('__file__')

    if filename is None:
        experiment_path = os.path.abspath(os.path.curdir)
        main = None
    else:
        main = Source.create(globs.get('__file__'))
        experiment_path = os.path.dirname(main.filename)
    return experiment_path, main


def iterate_imported_modules(globs):
    checked_modules = set(MODULE_BLACKLIST)
    for glob in globs.values():
        if isinstance(glob, module):
            mod_path = glob.__name__
        elif hasattr(glob, '__module__'):
            mod_path = glob.__module__
        else:
            continue  # pragma: no cover

        if not mod_path:
            continue

        for modname in iter_prefixes(mod_path):
            if modname in checked_modules:
                continue
            checked_modules.add(modname)
            mod = sys.modules.get(modname)
            if mod is not None:
                yield modname, mod


def iterate_all_python_files(base_path):
    # TODO support ignored directories/files
    for dirname, subdirlist, filelist in os.walk(base_path):
        if '__pycache__' in dirname:
            continue
        for filename in filelist:
            if filename.endswith('.py'):
                yield os.path.join(base_path, dirname, filename)


def iterate_sys_modules():
    items = list(sys.modules.items())
    for modname, mod in items:
        if modname not in MODULE_BLACKLIST and mod is not None:
            yield modname, mod


def get_sources_from_modules(module_iterator, base_path):
    sources = set()
    for modname, mod in module_iterator:
        if not hasattr(mod, '__file__'):
            continue

        filename = os.path.abspath(mod.__file__)
        if filename not in sources and \
                is_local_source(filename, modname, base_path):
            s = Source.create(filename)
            sources.add(s)
    return sources


def get_dependencies_from_modules(module_iterator, base_path):
    dependencies = set()
    for modname, mod in module_iterator:
        if hasattr(mod, '__file__') and is_local_source(
                os.path.abspath(mod.__file__), modname, base_path):
            continue

        try:
            pdep = PackageDependency.create(mod)
            if '.' not in pdep.name or pdep.version is not None:
                dependencies.add(pdep)
        except AttributeError:
            pass
    return dependencies


def get_sources_from_sys_modules(globs, base_path):
    return get_sources_from_modules(iterate_sys_modules(), base_path)


def get_sources_from_imported_modules(globs, base_path):
    return get_sources_from_modules(iterate_imported_modules(globs), base_path)


def get_sources_from_local_dir(globs, base_path):
    return {Source.create(filename)
            for filename in iterate_all_python_files(base_path)}


def get_dependencies_from_sys_modules(globs, base_path):
    return get_dependencies_from_modules(iterate_sys_modules(), base_path)


def get_dependencies_from_imported_modules(globs, base_path):
    return get_dependencies_from_modules(iterate_imported_modules(globs),
                                         base_path)


def get_dependencies_from_pkg(globs, base_path):
    dependencies = set()
    for dist in pkg_resources.working_set:
        dependencies.add(PackageDependency(dist.key, dist.version))
    return dependencies


source_discovery_strategies = {
    'none': lambda globs, path: set(),
    'imported': get_sources_from_imported_modules,
    'sys': get_sources_from_sys_modules,
    'dir': get_sources_from_local_dir
}

dependency_discovery_strategies = {
    'none': lambda globs, path: set(),
    'imported': get_dependencies_from_imported_modules,
    'sys': get_dependencies_from_sys_modules,
    'pkg': get_dependencies_from_pkg
}


def gather_sources_and_dependencies(globs, interactive=False):
    """Scan the given globals for modules and return them as dependencies."""

    experiment_path, main = get_main_file(globs)

    gather_sources = source_discovery_strategies[SETTINGS['DISCOVER_SOURCES']]
    sources = gather_sources(globs, experiment_path)
    sources.add(main)

    gather_dependencies = dependency_discovery_strategies[
        SETTINGS['DISCOVER_DEPENDENCIES']]
    dependencies = gather_dependencies(globs, experiment_path)

    if opt.has_numpy:
        # Add numpy as a dependency because it might be used for randomness
        dependencies.add(PackageDependency.create(opt.np))

    return main, sources, dependencies
