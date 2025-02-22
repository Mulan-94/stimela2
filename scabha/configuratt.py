import os.path
import importlib
import hashlib
import pathlib
import datetime
import re
import subprocess
import dill as pickle
from collections import OrderedDict
from collections.abc import Sequence

import uuid
from dataclasses import make_dataclass

from omegaconf.omegaconf import OmegaConf
from omegaconf.dictconfig import DictConfig
from omegaconf.listconfig import ListConfig
from omegaconf.errors import OmegaConfBaseException
from typing import Any, List, Dict, Optional, OrderedDict, Union, Callable

from yaml.error import YAMLError

class ConfigurattError(RuntimeError):
    pass


def _lookup_nameseq(name_seq: List[str], source_dict: Dict):
    """Internal helper: looks up nested item ('a', 'b', 'c') in a nested dict

    Parameters
    ----------
    name_seq : List[str]
        sequence of keys to look up
    source_dict : Dict
        nested dict

    Returns
    -------
    Any
        value if found, else None
    """
    source = source_dict
    names = list(name_seq)
    while names:
        source = source.get(names.pop(0), None)
        if source is None:
            return None
    return source        


def _lookup_name(name: str, *sources: List[Dict]):
    """Internal helper: looks up a nested item ("a.b.c") in a list of dicts

    Parameters
    ----------
    name : str
        section name to look up, e.g. "a.b.c"

    Returns
    -------
    Any
        first matching item found

    Raises
    ------
    NameError
        if matching item is not found
    """
    name_seq = name.split(".")
    for source in sources:
        result = _lookup_nameseq(name_seq, source)
        if result is not None:
            return result
    raise NameError(f"unknown key {name}")


def _flatten_subsections(conf, depth: int = 1, sep: str = "__"):
    """Recursively flattens subsections in a DictConfig (modifying in place)
    A structure such as
        a:
            b: 1
            c: 2
    Becomes
        a__b: 1
        a__c: 2

    Args:
        conf (DictConfig): config to flatten
        depth (int):       depth to which to flatten. Default is 1 level.
        sep (str):         separator to use, default is "__"
    """
    subsections = [(key, value) for key, value in conf.items() if isinstance(value, DictConfig)]
    for name, subsection in subsections:
        conf.pop(name)
        if depth > 1:
            _flatten_subsections(subsection, depth-1, sep)
        for key, value in subsection.items():
            conf[f"{name}{sep}{key}"] = value


def _resolve_config_refs(conf, pathname: str, location: str, name: str, includes: bool, use_sources: Optional[List[DictConfig]], 
                        selfrefs: bool = True, 
                        include_path: Optional[str]=None):
    """Resolves cross-references ("_use" and "_include" statements) in config object

    Parameters
    ----------
    conf : OmegaConf object
        input configuration object
    pathname : str
        full path to this confiog (directory component of that is used for _includes)
    location : str
        location of this configuration section, used for messages
    name : str
        name of this configuration file, used for messages
    includes : bool
        If True, "_include" references will be processed
    use_sources : optional list of OmegaConf objects
        one or more config object(s) in which to look up "_use" references. None to disable
    selfrefs (bool, optional): If False, "_use" references will only be looked up in existing config.
        If True (default), they'll also be looked up within the loaded config.
    include_path (str, optional):
        if set, path to each config file will be included in the section as element 'include_path'

    Returns
    -------
    Tuple of (conf, dependencies)
    conf : OmegaConf object    
        This may be a new object if a _use key was resolved, or it may be the existing object
    dependencies : OrderedDict
        Set of filenames that were _included

    Raises
    ------
    ConfigurattError
        If a _use or _include directive is malformed
    """
    errloc = f"config error at {location or 'top level'} in {name}"
    dependencies = OmegaConf.create()

    if isinstance(conf, DictConfig):

        # since _use and _include statements can be nested, keep on processing until all are resolved        
        updated = True
        recurse = 0
        flatten = conf.get("_flatten", 0)
        flatten_sep = conf.get("_flatten_sep", "__")
        for key in "_flatten", "_flatten_sep":
            if key in conf:
                del conf[key]
        
        while updated:
            updated = False
            # check for infinite recursion
            recurse += 1
            if recurse > 20:
                raise ConfigurattError(f"{errloc}: recursion limit exceeded, check your _use and _include statements")

            # handle _include entries
            if includes:
                include_files = conf.get("_include", None)
                if include_files:
                    del conf["_include"]
                    updated = True
                    if isinstance(include_files, str):
                        include_files = [include_files]
                    elif not isinstance(include_files, (tuple, list, ListConfig)) or not all(isinstance(x, str) for x in include_files):
                        raise ConfigurattError(f"{errloc}: _include: must be a string or a list of strings")

                    # load includes
                    accum_incl_conf = OmegaConf.create()
                    for incl in include_files:
                        if not incl:
                            raise ConfigurattError(f"{errloc}: empty _include specifier")

                        # check for (module)filename.yaml style
                        match = re.match("^\\((.+)\\)(.+)$", incl)
                        if match:
                            modulename, filename = match.groups()
                            try:
                                mod = importlib.import_module(modulename)
                            except ImportError as exc:
                                raise ConfigurattError(f"{errloc}: _include {incl}: can't import {modulename} ({exc})")

                            filename = os.path.join(os.path.dirname(mod.__file__), filename)
                            if not os.path.exists(filename):
                                raise ConfigurattError(f"{errloc}: _include {incl}: {filename} does not exist")

                        # absolute path -- one candidate
                        elif os.path.isabs(incl):
                            if not os.path.exists(incl):
                                raise ConfigurattError(f"{errloc}: _include {incl} does not exist")
                            filename = incl
                        # relative path -- scan PATH for candidates
                        else:
                            paths = [os.path.dirname(pathname)] + PATH
                            candidates = [os.path.join(p, incl) for p in paths] 
                            for filename in candidates:
                                if os.path.exists(filename):
                                    break
                            else:
                                raise ConfigurattError(f"{errloc}: _include {incl} not found in {':'.join(paths)}")

                        # load included file
                        incl_conf, deps = load(filename, location=location, 
                                            name=f"{filename}, included from {name}",
                                            includes=True, 
                                            use_sources=None)   # do not expand _use statements in included files, this is done below

                        dependencies.update(deps)
                        if include_path is not None:
                            incl_conf[include_path] = filename

                        # flatten structure
                        if flatten:
                            _flatten_subsections(incl_conf, flatten, flatten_sep)

                        # accumulate included config so that later includes override earlier ones
                        accum_incl_conf = OmegaConf.unsafe_merge(accum_incl_conf, incl_conf)
                    
                    # merge: our section overrides anything that has been included
                    conf = OmegaConf.unsafe_merge(accum_incl_conf, conf)

            # handle _use entries
            if use_sources is not None:
                merge_sections = conf.get("_use", None)
                if merge_sections:
                    del conf["_use"]
                    updated = True
                    if type(merge_sections) is str:
                        merge_sections = [merge_sections]
                    elif not isinstance(merge_sections, Sequence):
                        raise TypeError(f"invalid {name}._use field of type {type(merge_sections)}")
                    if len(merge_sections):
                        # convert to actual sections
                        merge_sections = [_lookup_name(name, *use_sources) for name in merge_sections]
                        # merge them all together
                        base = merge_sections[0].copy()
                        base.merge_with(*merge_sections[1:])
                        # resolve references before flattening
                        base, deps = _resolve_config_refs(base, pathname=pathname, name=name, 
                                                location=f"{location}._use" if location else "_use", 
                                                includes=includes, 
                                                use_sources=None if use_sources is None else ([conf] + use_sources if selfrefs else use_sources), 
                                                include_path=include_path)
                        dependencies.update(deps)
                        if flatten:
                            _flatten_subsections(base, flatten, flatten_sep)
                        base.merge_with(conf)
                        conf = base

        # recurse into content
        for key, value in conf.items_ex(resolve=False):
            if isinstance(value, (DictConfig, ListConfig)):
                value1, deps = _resolve_config_refs(value, pathname=pathname, name=name, 
                                                location=f"{location}.{key}" if location else key, 
                                                includes=includes, 
                                                use_sources=None if use_sources is None else ([conf] + use_sources if selfrefs else use_sources), 
                                                include_path=include_path)
                dependencies.update(deps)
                # reassigning is expensive, so only do it if there was an actual change 
                if value1 is not value:
                    conf[key] = value1

    # recurse into lists
    elif isinstance(conf, ListConfig):
        # recurse in
        for i, value in enumerate(conf._iter_ex(resolve=False)):
            if isinstance(value, (DictConfig, ListConfig)):
                value1, deps = _resolve_config_refs(value, pathname=pathname, name=name, 
                                                location=f"{location or ''}[{i}]", 
                                                includes=includes, 
                                                use_sources=None if use_sources is None else ([conf] + use_sources if selfrefs else use_sources), 
                                                include_path=include_path)
                dependencies.update(deps)
                if value1 is not value:
                    conf[i] = value

    return conf, dependencies


# paths to search for _include statements
PATH = ['.']

# path for cache
CACHEDIR = os.path.expanduser("~/.cache/configuratt")

# package version info stored with code dependencies
PACKAGE_VERSION = None


def _compute_hash(filelist, extra_keys):
    filelist = list(filelist) + list(extra_keys)
    return hashlib.md5(" ".join(filelist).encode()).hexdigest()


_git_cache = {}

def add_dependency(deps: OrderedDict, filename: str, **extra_attrs):
    depinfo = OmegaConf.create()
    depinfo.mtime     = os.path.getmtime(filename) 
    depinfo.mtime_str = datetime.datetime.fromtimestamp(depinfo.mtime).strftime('%c')
    depinfo.md5hash   = hashlib.md5(open(filename, "rb").read()).hexdigest()
    depinfo.update(**extra_attrs)
    deps[filename] = depinfo
    # add git info
    realdir = os.path.dirname(os.path.realpath(filename))
    # check cache first
    if realdir in _git_cache:
        depinfo.git = _git_cache[realdir]
    try:
        branches = subprocess.check_output("git -c color.ui=never branch -a -v -v".split(), cwd=realdir)
    except subprocess.CalledProcessError as exc:
        return None
    # use git to get the info
    gitinfo = OmegaConf.create()
    for line in branches.decode().split("\n"):
        line = line.strip()
        if line.startswith("*"):
            gitinfo.branch = line[1:].strip()
            break
    # get description
    try:
        describe = subprocess.check_output("git describe --abbrev=16 --always --long --all".split(), cwd=realdir)
        gitinfo.describe = describe.decode().strip()
    except subprocess.CalledProcessError as exc:
        pass
    # get remote info
    try:
        remotes = subprocess.check_output("git remote -v".split(), cwd=realdir)
        gitinfo.remotes = remotes.decode().strip().split('\n')
    except subprocess.CalledProcessError as exc:
        pass

    deps[filename].git = _git_cache['git'] = gitinfo


def load_cache(filelist: List[str], extra_keys=[], verbose=None):
    filehash = _compute_hash(filelist, extra_keys)
    if not os.path.isdir(CACHEDIR):
        if verbose:
            print(f"{CACHEDIR} does not exist")
        return None, None
    filename = os.path.join(CACHEDIR, filehash)
    if not os.path.exists(filename):
        if verbose:
            print(f"hash file {filename} does not exist")
        return None, None
    # check that all configs are older than the cache
    cache_mtime = os.path.getmtime(filename)
    for f in filelist:
        if os.path.getmtime(f) > cache_mtime:
            if verbose:
                print(f"Config {f} is newer than the cache, forcing reload")
            return None, None
    # load cache
    try:
        conf, deps = pickle.load(open(filename, 'rb'))
        if not isinstance(deps, DictConfig):
            raise TypeError(f"cached deps object is of type {type(deps)}, expecting DictConfig")
    except Exception as exc:
        print(f"Error loading cached config from {filename}: {exc}. Removing the cache.")
        os.unlink(filename)
        return None, None
    # check that all dependencies are older than the cache
    for f in deps:
        if not os.path.exists(f):
            if verbose:
                print(f"Dependency {f} doesn't exist, forcing reload")
            return None, None
        if os.path.getmtime(f) > cache_mtime:
            if verbose:
                print(f"Dependency {f} is newer than the cache, forcing reload")
            return None, None
    if verbose:
        print(f"Loaded cached config for {' '.join(filelist)} from {filename}")    
    return conf, deps


def save_cache(filelist: List[str], conf, deps, extra_keys=[], verbose=False):
    pathlib.Path(CACHEDIR).mkdir(parents=True, exist_ok=True)
    filelist = list(filelist)   # add self to dependencies
    filehash = _compute_hash(filelist, extra_keys)
    filename = os.path.join(CACHEDIR, filehash)
    # add ourselves to dependencies, so that cache is cleared if implementation changes
    add_dependency(deps, __file__, version=PACKAGE_VERSION)
    pickle.dump((conf, deps), open(filename, "wb"), 2)
    if verbose:
        print(f"Caching config for {' '.join(filelist)} as {filename}")


def load(path: str, use_sources: Optional[List[DictConfig]] = [], name: Optional[str]=None, location: Optional[str]=None, 
          includes: bool=True, selfrefs: bool=True, include_path: str=None, 
          use_cache: bool = True, verbose: bool = False):
    """Loads config file, using a previously loaded config to resolve _use references.

    Args:
        path (str): path to config file
        use_sources (Optional[List[DictConfig]]): list of existing configs to be used to resolve "_use" references, or None to disable
        name (Optional[str]): name of this config file, used for error messages
        location (Optional[str]): location where this config is being loaded (if not at root level)
        includes (bool, optional): If True (default), "_include" references will be processed
        selfrefs (bool, optional): If False, "_use" references will only be looked up in existing config.
            If True (default), they'll also be looked up within the loaded config.
        include_path (str, optional):
            if set, path to each config file will be included in the section as element 'include_path'

    Returns:
        Tuple of (conf, dependencies)
            conf (DictConfig): config object    
            dependencies (OrderedDict): filenames that were _included
    """
    conf, deps = load_cache((path,), verbose=verbose) if use_cache else (None, None)

    if conf is None:
        subconf = OmegaConf.load(path)
        name = name or os.path.basename(path)
        deps = OmegaConf.create()
        add_dependency(deps, path)
        conf, deps = _resolve_config_refs(subconf, pathname=path, location=location, name=name, includes=includes, use_sources=use_sources, include_path=include_path)
        if use_cache:
            save_cache((path,), conf, deps, verbose=verbose)

    return conf, deps


def load_nested(filelist: List[str], 
                structured: Optional[DictConfig] = None,
                typeinfo = None,
                use_sources: Optional[List[DictConfig]] = [],
                location: Optional[str] = None,  
                nameattr: Union[Callable, str, None] = None,
                config_class: Optional[str] = None,
                include_path: Optional[str] = None,
                use_cache: bool = True,
                verbose: bool = False):
    """Builds nested configuration from a set of YAML files corresponding to sub-sections

    Parameters
    ----------
    conf : OmegaConf object
        root OmegaConf object to merge content into
    filelist : List[str]
        list of subsection config files to load
    schema : Optional[DictConfig]
        schema to be applied to each file, if any
    use_sources : Optional[List[DictConfig]]
        list of existing configs to be used to resolve "_use" references, or None to disable
    location : Optional[str]
        if set, contents of files are being loaded under 'location.subsection_name'. If not set, then 'subsection_name' is being
        loaded at root level. This is used for correctly formatting error messages and such.
    nameattr : Union[Callable, str, None]
        if None, subsection_name will be taken from the basename of the file. If set to a string such as 'name', will set 
        subsection_name from that field in the subsection config. If callable, will be called with the subsection config object as a single 
        argument, and must return the subsection name
    config_class : Optional[str]
        name of config dataclass to form (when using typeinfo), if None, then generated automatically
    include_path : Optional[str] 
        if set, path to each config file will be included in the section as element 'include_path'

    Returns
    -------
        Tuple of (conf, dependencies)
            conf (DictConfig): config object    
            dependencies (set): set of filenames that were _included

    Raises
    ------
    NameError
        If subsection name is not resolved
    """
    section_content, dependencies = load_cache(filelist, verbose=verbose) if use_cache else (None, None)

    if section_content is None:
        section_content = {} # OmegaConf.create()
        dependencies = OmegaConf.create()

        for path in filelist:
            # load file
            subconf, deps = load(path, location=location, use_sources=use_sources, include_path=include_path)
            dependencies.update(deps)
            if include_path:
                subconf[include_path] = path

            # figure out section name
            if nameattr is None:
                name = os.path.splitext(os.path.basename(path))[0]
            elif callable(nameattr):
                name = nameattr(subconf) 
            elif nameattr in subconf:
                name = subconf.get(nameattr)
            else:
                raise NameError(f"{path} does not contain a '{nameattr}' field")

            # # resolve _use and _include statements
            # try:
            #     subconf = resolve_config_refs(subconf, f"{location}.{name}" if location else name, conf, subconf))
            # except (OmegaConfBaseException, YAMLError) as exc:
            #     raise ConfigurattError(f"config error in {path}: {exc}")

            # apply schema
            if structured is not None:
                try:
                    subconf = OmegaConf.merge(structured, subconf) 
                except (OmegaConfBaseException, YAMLError) as exc:
                    raise ConfigurattError(f"schema error in {path}: {exc}")

            section_content[name] = subconf

        if structured is None and typeinfo is not None:
            if config_class is None:
                config_class = "ConfigClass_" + uuid.uuid4().hex
            fields = [(name, typeinfo) for name in section_content.keys()]
            datacls = make_dataclass(config_class, fields)
            # datacls.__module__ == __name__  # for pickling
            structured = OmegaConf.structured(datacls)
            section_content = OmegaConf.merge(structured, section_content)
    
        if use_cache:
            save_cache(filelist, section_content, dependencies, verbose=verbose)

    return section_content, dependencies


