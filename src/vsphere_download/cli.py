"""Copy files/directories out of Windows VMs on vSphere through snapshot disk copies."""

import argparse
import getpass
import os
import ssl
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

from vsphere_download.banner import banner
from vsphere_download.cleanup import confirm, sweep
from vsphere_download.datastore import DatastoreReader
from vsphere_download.guestpath import GuestPath, parse_guest_path
from vsphere_download.inventory import is_relevant, iter_vm_properties
from vsphere_download.report import TIMESTAMP_FORMAT, print_vm
from vsphere_download.workflow import process_vm


def parse_args() -> argparse.Namespace:
    # The banner carries the version, so show it only for --help/--version.
    if {"-h", "--help", "--version"}.intersection(sys.argv[1:]):
        print(f"\n{banner()}\n")
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version="", help="show version and exit"
    )
    parser.add_argument(
        "-s",
        "--server",
        required=True,
        help="vSphere server (vCenter or standalone ESXi)",
    )
    parser.add_argument(
        "-o", "--port", type=int, default=443, help="port to connect to"
    )
    parser.add_argument("-u", "--user", required=True, help="user name")
    parser.add_argument("-p", "--password", help="password (prompted for if omitted)")
    parser.add_argument(
        "-k",
        "--disable-ssl-verification",
        action="store_true",
        help="disable SSL certificate verification",
    )
    parser.add_argument(
        "--vm",
        required=True,
        help="name of the VM to act on, matched exactly (case-sensitive); "
        "VMs of the same name in different folders are all matched",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="SOURCE",
        help="path(s) on the guest's system drive to copy out, e.g. "
        r"'C:\Windows\System32\drivers\etc\hosts'; a directory is copied "
        "with everything below it (not needed with --cleanup)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="do not copy; instead list this tool's leftover snapshots and disk "
        "copies on the matched VMs (from a run that crashed or was interrupted)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="with --cleanup, delete the leftovers instead of only listing them",
    )
    args = parser.parse_args()
    if args.delete and not args.cleanup:
        parser.error("--delete only applies together with --cleanup")
    if not args.vm.strip():
        parser.error("--vm must not be empty; it selects which VMs to act on")
    if args.cleanup:
        if args.paths:
            parser.error("--cleanup only sweeps leftovers; it takes no SOURCE paths")
    else:
        if not args.paths:
            parser.error("name at least one SOURCE path to copy out of the VMs")
        try:
            args.sources = [parse_guest_path(p) for p in args.paths]
        except ValueError as e:
            parser.error(str(e))
    if not args.password:
        args.password = getpass.getpass(f"Password for {args.user}@{args.server}: ")
    return args


def main() -> None:
    try:
        run()
    except KeyboardInterrupt:
        # Whatever was interrupted has cleaned up and said so already.
        print("\nInterrupted.")
        raise SystemExit(130) from None


def run() -> None:
    args = parse_args()
    # The copied files are written below the current directory; a cleanup
    # sweep writes nothing.
    if not args.cleanup and not os.access(Path.cwd(), os.W_OK):
        raise SystemExit(f"Cannot write to the current directory {Path.cwd()}")
    si = SmartConnect(
        host=args.server,
        port=args.port,
        user=args.user,
        pwd=args.password,
        disableSslCertValidation=args.disable_ssl_verification,
    )
    try:
        vms = [vm for vm in iter_vm_properties(si) if is_relevant(vm, args.vm)]
        for vm in vms:
            print_vm(vm)
        print(f"\nFound {len(vms)} matching Windows VM(s).\n")
        if args.cleanup:
            sweep(si, vms, args.delete)
        elif vms:
            if not confirm(
                f"Take a snapshot and copy files from these {len(vms)} VM(s)?"
            ):
                print("Aborted.")
                return
            copy_vms(si, args, vms)
    finally:
        Disconnect(si)


def copy_vms(
    si: vim.ServiceInstance, args: argparse.Namespace, vms: list[dict[str, Any]]
) -> None:
    context = (
        ssl._create_unverified_context()
        if args.disable_ssl_verification
        else ssl.create_default_context()
    )
    reader = DatastoreReader(args.server, args.port, si._stub.cookie, context)
    # Names this run's snapshots and output folders.
    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    sources: list[GuestPath] = args.sources
    failed = []
    try:
        for vm in vms:
            name = vm.get("name", "?")
            try:
                ok = process_vm(si, reader, vm, sources, Path.cwd(), timestamp)
            except Exception as e:
                # process_vm has cleaned up already; go on with the next VM.
                print(f"{name}: unexpected error: {e!r}")
                ok = False
            if not ok:
                failed.append(name)
    finally:
        reader.close()
    print(f"\nCopied files out of {len(vms) - len(failed)} of {len(vms)} VM(s).")
    if failed:
        # A VM can fail after its snapshot or a copy was made; point at the
        # sweep so any leftover can be checked and removed.
        print(f"To check for leftovers: {prog()} --cleanup --vm {args.vm!r}")
        raise SystemExit(f"Failed: {', '.join(failed)}")


def prog() -> str:
    return Path(sys.argv[0]).name or "vsphere-download"
