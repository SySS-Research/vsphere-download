"""Finding VMs and their place in the vSphere inventory."""

from collections.abc import Iterator
from typing import Any

from pyVmomi import vim, vmodl

# Properties fetched for every VM in a single PropertyCollector round trip.
# Missing keys in the result mean the property is unset (e.g. no VMware Tools).
VM_PROPERTIES = [
    "name",
    "config.guestId",
    "config.guestFullName",
    "guest.guestFamily",
    "runtime.powerState",
    "snapshot",
]


def iter_vm_properties(si: vim.ServiceInstance) -> Iterator[dict[str, Any]]:
    """Yield a property dict for every VM in the inventory."""
    content = si.RetrieveContent()
    assert content.viewManager is not None
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        pc = vmodl.query.PropertyCollector
        filter_spec = pc.FilterSpec(
            objectSet=[
                pc.ObjectSpec(
                    obj=view,
                    skip=True,
                    selectSet=[
                        pc.TraversalSpec(
                            name="traverseEntities",
                            path="view",
                            skip=False,
                            type=vim.view.ContainerView,
                        )
                    ],
                )
            ],
            propSet=[pc.PropertySpec(type=vim.VirtualMachine, pathSet=VM_PROPERTIES)],
        )
        collector = content.propertyCollector
        result = collector.RetrievePropertiesEx([filter_spec], pc.RetrieveOptions())
        while result:
            for obj in result.objects:
                props = {prop.name: prop.val for prop in obj.propSet or []}
                props["obj"] = obj.obj
                yield props
            if not result.token:
                break
            result = collector.ContinueRetrievePropertiesEx(result.token)
    finally:
        view.Destroy()


def is_windows(vm: dict[str, Any]) -> bool:
    # Guest family is reported by VMware Tools; the configured guest ID
    # (e.g. "windows2019srv_64Guest", "winNetStandardGuest") covers VMs
    # without running Tools.
    return vm.get("guest.guestFamily") == "windowsGuest" or str(
        vm.get("config.guestId", "")
    ).startswith("win")


def is_relevant(vm: dict[str, Any], name: str) -> bool:
    return is_windows(vm) and vm.get("name") == name


def find_datacenter(entity: vim.ManagedEntity) -> vim.Datacenter:
    node: vim.ManagedEntity | None = entity
    while node is not None and not isinstance(node, vim.Datacenter):
        node = node.parent
    if node is None:
        raise ValueError(f"{entity.name}: not in a datacenter")
    return node


def inventory_path(entity: vim.ManagedEntity) -> str:
    """Path of an entity below the root folder, e.g. "Folder/Datacenter"."""
    names = []
    node: vim.ManagedEntity | None = entity
    # The root folder is the only entity without a parent; it is not part of
    # the path.
    while node is not None and node.parent is not None:
        names.append(node.name)
        node = node.parent
    return "/".join(reversed(names))
