"""Banner shown for --help and --version."""

from importlib.metadata import version


def banner() -> str:
    title = "vsphere-download"
    author = "Stefan Walter, SySS GmbH"
    v = version("vsphere-download")
    tagline = f"v{v}  -  copy files out of Windows VMs on vSphere"
    # Right-align the author with the end of the tagline.
    gap = max(2, len(tagline) - len(title) - len(author))
    return f"{title}{' ' * gap}{author}\n{tagline}"
