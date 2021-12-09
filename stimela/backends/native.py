import shlex, re
import importlib, traceback, sys
from typing import Dict, Optional, Any
from scabha.cargo import Cab
from stimela import logger
from stimela.utils.xrun_poll import xrun
from stimela.exceptions import StimelaCabRuntimeError
import click


def run(cab: Cab, log, subst: Optional[Dict[str, Any]] = None):
    match = re.match("^\((.+)\)(.+)$", cab.command)

    if match:
        return run_callable(match.group(1), match.group(2), cab, log, subst)

    else:
        return run_command(cab, log, subst)


def run_callable(modulename, funcname, cab, log, subst: Optional[Dict[str, Any]] = None):
    try:
        mod = importlib.import_module(modulename)
    except ImportError as exc:
        raise StimelaCabRuntimeError(f"can't import {modulename}: {exc}", log=log)

    func = getattr(mod, funcname, None)

    if not callable(func):
        raise StimelaCabRuntimeError(f"{modulename}.{funcname} is not a valid callable", log=log)

    if isinstance(func, click.Command):
        log.info(f"invoking click command {modulename}.{funcname}()")
        func = func.callback
    else:
        log.info(f"invoking callable {modulename}.{funcname}()")

    try:
        retval = func(**cab.params)
    except Exception as exc:
        for line in traceback.format_exception(*sys.exc_info()):
            log.error(line.rstrip())
        raise StimelaCabRuntimeError(f"{modulename}.{funcname}() threw exception: {exc}'", log=log)

    log.info(f"{modulename}.{funcname}() returns {retval}")

    return retval


def run_command(cab: Cab, log, subst: Optional[Dict[str, Any]] = None):
    args, venv = cab.build_command_line(subst)

    command_name = args[0]

    if venv:
        args = ["/bin/bash", "--rcfile", f"{venv}/bin/activate", "-c", " ".join(shlex.quote(arg) for arg in args)]

    log.debug(f"command line is {args}")
    
    cab.reset_runtime_status()

    retcode = xrun(args[0], args[1:], shell=False, log=log, 
                output_wrangler=cab.apply_output_wranglers, 
                return_errcode=True, command_name=command_name)

    # if retcode is not 0, and cab didn't declare itself a success, raise error
    if retcode:
        if not cab.runtime_status:
            raise StimelaCabRuntimeError(f"{command_name} returned non-zero exit status {retcode}", log=log)
    else:
        if cab.runtime_status is False:
            raise StimelaCabRuntimeError(f"{command_name} was marked as failed based on its output", log=log)

    return retcode
