Copy files and directories out of running Windows VMs on vSphere hypervisors.

# vsphere-download

Given a path on a Windows guest's system drive, the tool snapshots the VM,
copies its disk on the datastore (to allow access via HTTP API), reads the copy
over HTTP range requests, parses the VMDK disk and Windows NTFS volume using
[dissect](https://github.com/fox-it/dissect), and copies the requested files
out.

> [!NOTE]
> **Intended for authorized use only.**

---

## Prerequisites

Access to a vSphere server and credentials with permission to snapshot the
target VMs and read their datastores.

## Install

```bash
uv tool install git+https://github.com/SySS-Research/vsphere-download
```

## Usage

```console
$ vsphere-download --help
vsphere-download                            Stefan Walter, SySS GmbH
v0.0.1.dev60+g229665aa1  -  copy files out of Windows VMs on vSphere

usage: vsphere-download [-h] [--version] -s SERVER [-o PORT] -u USER
                        [-p PASSWORD] [-k] --vm VM [-c CONNECTIONS]
                        [--cleanup] [--delete]
                        [SOURCE ...]

Copy files and directories out of Windows VMs on vSphere/ESXi through snapshot
disk copies.

positional arguments:
  SOURCE                path(s) on the guest's system drive to copy out, e.g.
                        'C:\Windows\System32\drivers\etc\hosts'; a directory
                        is copied with everything below it (not needed with
                        --cleanup)

options:
  -h, --help            show this help message and exit
  --version             show version and exit
  -s, --server SERVER   vSphere server (vCenter or standalone ESXi)
  -o, --port PORT       port to connect to
  -u, --user USER       user name
  -p, --password PASSWORD
                        password (prompted for if omitted)
  -k, --disable-ssl-verification
                        disable SSL certificate verification
  --vm VM               name of the VM to act on, matched exactly (case-
                        sensitive); VMs of the same name in different folders
                        are all matched
  --cleanup             do not copy; instead list this tool's leftover
                        snapshots and disk copies on the matched VMs (from a
                        run that crashed or was interrupted)
  --delete              with --cleanup, delete the leftovers instead of only
                        listing them
```

### Specifying what to copy

Each `SOURCE` is a path on the guest's **system drive** (`C:`). The drive
letter is optional and both separators work, so these are all accepted:

```
C:\Windows\System32\drivers\etc\hosts
\Windows\System32\drivers\etc\hosts
Windows/System32/drivers/etc/hosts
```

A file is copied out as a single file; a directory is copied recursively with
everything below it. Links and junctions are reported and not followed, so a
copy never recurses into itself or outside the tree. Only the Windows system
volume is read, so other drive letters are not supported.

Results are written to a per-VM folder in the current directory, named after
the VM and the run timestamp.

### Example: copy a directory

Copy the whole `etc` directory, with everything below it, out of the VM
`dc01`:

```bash
vsphere-download -s SERVER -u USER --vm dc01 \
    'C:\Windows\System32\drivers\etc'
```

The directory ends up as `dc01_<timestamp>/etc/` in the current directory,
holding `hosts`, `networks`, `services` and the other files from the guest.

```console
$ vsphere-download -s localhost -o 443 -u myuser -p "$PASS" -k --vm #####_AD_dc01 '\Windows\System32\drivers\etc\'
----------------------------------------------------------------------
Name:               #####_AD_dc01
Power state:        poweredOn
Guest OS (config):  Microsoft Windows Server 2019 (64-bit)
Guest ID:           windows2019srv_64Guest
Snapshots:          -

Found 1 matching Windows VM(s).

Take a snapshot and copy files from these 1 VM(s)? [y/N] y
#####_AD_dc01: creating snapshot 2026-10-12_07-39-41_vsphere-download ... done
#####_AD_dc01: copying [FASTRAID1] #####_AD_dc01/#####_AD_dc01.vmdk to [FASTRAID1] #####_AD_dc01/2026-10-12_07-39-41_vsphere-download-copy-2000.vmdk ... done
#####_AD_dc01: deleting snapshot 2026-10-12_07-39-41_vsphere-download ... done
[FASTRAID1] #####_AD_dc01/2026-10-12_07-39-41_vsphere-download-copy-2000.vmdk: reading: read 192.0 MiB in 50 request(s)
[FASTRAID1] #####_AD_dc01/2026-10-12_07-39-41_vsphere-download-copy-2000.vmdk: Windows found on partition 4
[FASTRAID1] #####_AD_dc01/2026-10-12_07-39-41_vsphere-download-copy-2000.vmdk: reading: read 212.0 MiB in 55 request(s)
#####_AD_dc01: wrote to /home/user/#####_AD_dc01_2026-10-12_07-39-41/: etc\ (5 files, 23907 bytes)
#####_AD_dc01: deleting [FASTRAID1] #####_AD_dc01/2026-10-12_07-39-41_vsphere-download-copy-2000.vmdk ... done

Copied files out of 1 of 1 VM(s).
```

Resulting tree:

```
└── etc
    ├── hosts
    ├── lmhosts.sam
    ├── networks
    ├── protocol
    └── services
```

### Example: copy a single file

Copy the hosts file out of the VM `dc01`, skipping certificate verification
for a server with a self-signed certificate:

```bash
vsphere-download -s SERVER -u USER -k --vm dc01 \
    'C:\Windows\System32\drivers\etc\hosts'
```

The file ends up as `dc01_<timestamp>/hosts` in the current directory.

### Example: Windows secrets

Copy out the registry hives and, where present, the AD database:

```bash
vsphere-download -s SERVER -u USER --vm dc01 \
    'C:\Windows\System32\config\SAM' \
    'C:\Windows\System32\config\SECURITY' \
    'C:\Windows\System32\config\SYSTEM' \
    'C:\Windows\NTDS\ntds.dit'
```

> [!NOTE]
> A domain controller has `ntds.dit`; on a workstation that path is simply
> reported as not found, and the hives are still copied.

### Checking for leftovers

A normal run deletes its snapshot and disk copies as it goes, but a crash or
an interrupted run can leave one behind. To list this tool's leftover
snapshots and disk copies on the matched VMs:

```bash
vsphere-download -s SERVER -u USER --vm VM --cleanup
```

This only reports; add `--delete` to actually remove what it finds. Both only
ever match snapshots and disks this tool created (they carry a
`vsphere-download` marker in their name), never an admin's own.

---

## Approach

**Advantages**

- **Works on running VMs** they only see the usual snapshot overhead
  and the extra read load of the copy.
- **No large file transfers** of complete disk images; only the requested
  ranges cross the network, and no full disk is downloaded.
- **Hypervisor-only** the guest should not notice anything.
- **No guest prerequisites** e.g. VMware Tools need not be installed.
- **No guest credentials** required.
- **No temporary VM** / attaching existing disk to new VM.

**Drawbacks**

- Requires free space on the datastore for the copy (roughly the disk's used
  space, up to its full size), plus the time and I/O of the copy. The copy
  is necessary for HTTP range access to the disk of a running VM.
- Does not support guest encryption (BitLocker) or vSphere Virtual Machine
  Encryption (tries to detect it and abort early in that case).

## Events / Logs

Note that this is **not stealthy**. All operations are logged in the
hypervisor. During my testing there were e.g. clear log entries for:

- Create virtual machine snapshot
- Copy virtual disk
- Remove snapshot
- Delete virtual disk

---

## Development

```bash
uv run pytest       # tests
uv run mypy         # type checking
uv run ruff check   # linting
```

## Acknowledgements

- My colleagues Jonas and Alexander for input and test lab environments.
- [synacktiv/fusetea](https://github.com/synacktiv/fusetea) a similar
  project with a more general / lower-level interface that composes better
  with other tools.
- This project was fully written using Claude Code.
