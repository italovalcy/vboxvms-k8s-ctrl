import asyncio
import pprint
import time
import random
import re

import kopf
import yaml


# TODO:
# - check if vms are running
# - check for vbox hostonlyif (on startup)
# - periodically check for VMS (on startup)

NAME_PFX = "vbox-vm-"
SETTINGS = {
    "max_vms": 20,
    "max_wait": 600,
}
TEMPLATES = {}
VMS = {}
VMS_BY_NAME = {}
VMS_BY_NAME_LOCK = asyncio.Lock()

async def sh(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT  # Redirect stderr to stdout
    )
    stdout_data, stderr_data = await proc.communicate()
    return stdout_data.decode().strip(), proc.returncode


async def update_settings(body):
    for k in SETTINGS:
        SETTINGS[k] = body.data.get(k, SETTINGS[k])


async def get_available_vm_name():
    for vm_id in range(SETTINGS["max_vms"]):
        if f"{NAME_PFX}{vm_id}" not in VMS_BY_NAME:
            return f"{NAME_PFX}{vm_id}"
    return None


async def get_vm_ip(name, logger):
    output, ret = await sh(f"vboxmanage showvminfo {name}")
    if ret != 0:
        logger.warning(f"Failed to get VM info: {output}")
        return

    if not (match := re.match(r".*MAC: ([0-9a-fA-F]+)", output, re.DOTALL)):
        logger.warning(f"MAC address not found: {output}")
        return

    mac = match.group(1)
    output, ret = await sh(f"VBoxManage dhcpserver findlease --network=HostInterfaceNetworking-vboxnet0 --mac-address={mac}")
    if ret != 0:
        logger.warning(f"Failed to get dhcpserver info for mac={mac}: {output}")
        return

    if not (match := re.match(r".*IP Address:\s*([0-9a-fA-F:.]+)", output, re.DOTALL)):
        logger.warning(f"IP address not found: {output}")
        return

    return match.group(1)


async def delete_vm(name):
    await sh(f"vboxmanage controlvm {name} poweroff")
    await sh(f"vboxmanage unregistervm {name} --delete-all")


async def create_vm(namespace, name, uid, image_name, image_tag):
    #output, ret = await sh(f"vboxmanage clonevm template-{image_name} --name={name} --register --options=link --snapshot={image_tag}")
    output, ret = await sh(f"vboxmanage clonevm template-{image_name} --name={name} --register --snapshot={image_tag}")
    if ret != 0:
        raise ValueError(f"Failed to create VM {output}")

    vrdeport = 5000 + int(name.strip(NAME_PFX))
    now = int(time.time())
    output, ret = await sh(f"vboxmanage modifyvm {name} --vrdemulticon on --vrdeport {vrdeport} --description='X-VBOX-CTL-uid={uid};X-VBOX-CTL-namespace={namespace};X-VBOX-CTL-name={name};X-VBOX-CTL-createdat={now}'")
    if ret != 0:
        raise ValueError(f"Failed to modify VM {output}")

    output, ret = await sh(f"vboxmanage startvm --type headless {name}")
    if ret != 0:
        raise ValueError(f"Failed to start VM {output}")


@kopf.on.startup()
async def startup_fn_simple(logger, **kwargs):
    logger.info('Starting vboxvmsctl..')
    logger.info('Checking for hostonlyif vboxnet0')
    output, ret = await sh(f"vboxmanage list hostonlyifs")
    if ret != 0:
        raise ValueError(f"Failed to list hostonlyifs ret={ret} output={output}")
    if "vboxnet0" not in output:
        logger.info('The hostonlyif vboxnet0 does not exists, creating it...')
        output, ret = await sh(f"VBoxManage hostonlyif create")
        if ret != 0:
            raise ValueError(f"Failed to create hostonlyif ret={ret} output={output}")
    logger.info('Checking for VM templates')
    output, ret = await sh(f"vboxmanage list vms")
    if ret != 0:
        raise ValueError(f"Failed to list VMs ret={ret} output={output}")
    output, ret = await sh(f'test -z "$VBOXVMSCTL_TEMPLATES_DIR" || for vm in $(ls -1 "$VBOXVMSCTL_TEMPLATES_DIR"); do vboxmanage registervm "$VBOXVMSCTL_TEMPLATES_DIR/$vm/$vm.vbox"; done')
    if ret != 0:
        raise ValueError(f"Failed to import VM templates ret={ret} output={output}")
    output, ret = await sh(f"vboxmanage list vms")
    if ret != 0:
        raise ValueError(f"Failed to list VMs ret={ret} output={output}")
    pattern = re.compile(r'"(?P<name>[^"]+)"\s+\{(?P<uuid>[0-9a-fA-F-]+)\}')
    vms = []
    for line in output.splitlines():
        if match := pattern.match(line):
            vms.append(match.groupdict())
    pattern_snapshot = re.compile(r'\s*Name:\s+([^\s]+)\s+')
    for vm in vms:
        if not vm["name"].startswith("template-"):
            logger.warning(f"VM name does not starts with 'template-', ignoring! vm={vm['name']}")
            continue
        output, ret = await sh(f"vboxmanage snapshot {vm['name']} list")
        if ret != 0:
            logger.warning(f"Failed to list VM snapshots vm={vm['name']} ret={ret} output={output}. Trying to create latest snapshot...")
            output, ret = await sh(f"vboxmanage snapshot {vm['name']} take latest")
            if ret != 0:
                logger.warning(f"Failed to create snapshot 'latest' for vm={vm['name']} ret={ret} output={output}. Ignoring this VM template!")
                continue
            logger.warning(f"Successful created 'latest' snapshot for vm={vm['name']}. Trying to list again")
            output, ret = await sh(f"vboxmanage snapshot {vm['name']} list")
            if ret != 0:
                logger.warning(f"Still failing to list VM snapshots vm={vm['name']} ret={ret} output={output}. Ignoring this VM template!")
                continue
        snapshots = []
        for line in output.splitlines():
            if match := pattern_snapshot.match(line):
                snapshots.append(match.group(1))
        TEMPLATES[vm["name"].lstrip("template-")] = snapshots
    logger.info(f"VM templates: {TEMPLATES}")
    logger.info(f"Started successfully!")


@kopf.on.cleanup()
async def cleanup_fn(logger, **kwargs):
    logger.info('Cleaning up in 3s...')
    await asyncio.sleep(3)


#@kopf.on.create('ConfigMap', field='metadata.name', value='settings')
#async def settings_configmap_created(body, logger, **kwargs):
#    update_settings(body)
#
#
#@kopf.on.update('ConfigMap', field='metadata.name', value='settings')
#async def settings_configmap_updated(body, logger, **kwargs):
#    update_settings(body)


@kopf.on.create("amlight.net", "v1", "vboxvms")
async def create(body, meta, spec, patch, logger, name, namespace, **kwargs):
    logger.info("Create body: %s" % (body))
    uid = body["metadata"]["uid"]
    async with VMS_BY_NAME_LOCK:
        if not (name := await get_available_vm_name()):
            patch.status["phase"] = "Failed"
            patch.status["detail"] = "Maximum number of VBox VMs exceeded"
            raise kopf.PermanentError("Maximum number of VMs exceeded.")
        VMS_BY_NAME[name] = uid
    image = body["spec"]["image"]
    image_name, image_tag = image.split(":") if ":" in image else (image, "latest")
    if image_tag not in TEMPLATES.get(image_name, []):
        patch.status["phase"] = "Failed"
        msg = f"Image name or tag not available. Available VMs/tags: {TEMPLATES}"
        patch.status["detail"] = msg
        raise kopf.PermanentError(msg)
    try:
        await create_vm(namespace, name, uid, image_name, image_tag)
    except Exception as exc:
        logger.info(f"Failed to create VM: {exc}. Force delete")
        await delete_vm(name)
        async with VMS_BY_NAME_LOCK:
            del VMS_BY_NAME[vm["name"]]
        raise kopf.TemporaryError("Failed to create VM. Retrying later..")
    VMS[uid] = {"body": body, "name": name}
    patch.status['phase'] = 'Pending'
    patch.spec["ip"] = "<none>"
    logger.info("returning status")
    return {'job1-status': 100}


@kopf.on.delete("amlight.net", "v1", "vboxvms")
async def delete(body, patch, logger, **kwargs):
    logger.info("Delete body: %s" % (body))
    uid = body["metadata"]["uid"]
    if vm := VMS.get(uid):
        await delete_vm(vm["name"])
        async with VMS_BY_NAME_LOCK:
            del VMS_BY_NAME[vm["name"]]
        del VMS[uid]
    else:
        logger.info(f"VM not found! uid={uid} VMs={VMS}")
    patch.status['phase'] = 'Succeeded'

@kopf.daemon("amlight.net", "v1", "vboxvms")
async def check_status(body, status, patch, logger, **kwargs):
    logger.info(f"Daemon for checking status body={body}...")
    uid = body["metadata"]["uid"]
    start = time.time()
    while time.time() - start <= SETTINGS["max_wait"]:
        if status.get("phase", "Pending") != "Pending":
            logger.info(f"Invalid status={status} for body={body}. Aborting...")
            break
        if vm := VMS.get(uid):
            ip = await get_vm_ip(vm["name"], logger)
            if ip:
                logger.info(f"Found IP for VM name={vm['name']} ip={ip}")
                patch.spec["ip"] = ip
                patch.status["phase"] = "Running"
                break
        else:
            logger.info(f"VM not found! uid={uid} VMs={VMS}")
        await asyncio.sleep(10)
    else:
        logger.info(f"Timeout waiting for VM to be ready! start={start} now={time.time()} uid={uid} body={body}")
        patch.status["phase"] = "Failed"
