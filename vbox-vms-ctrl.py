import asyncio
import os
import pprint
import signal
import time
import random
import re
import shlex
import ssl

import aiohttp
import kopf
import yaml


# TODO:
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

SH_TIMEOUT = 120
SH_TIMEOUT_EXIT = 124  # same as timeout(1)


async def sh(cmd, timeout=SH_TIMEOUT):
    """Run a shell command, returning (output, returncode).

    The command runs in its own process group and the whole group is killed on
    timeout (or cancellation), so a hung vboxmanage cannot block callers (some
    of which hold VMS_BY_NAME_LOCK) forever. On timeout returncode is 124.
    """
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # Redirect stderr to stdout
        start_new_session=True,
    )
    try:
        stdout_data, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return f"Timed out after {timeout}s: {cmd}", SH_TIMEOUT_EXIT
    return stdout_data.decode(errors="replace").strip(), proc.returncode


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


# VirtualBox VM states in which the guest is not (or no longer) executing
VM_DEAD_STATES = {"poweroff", "aborted", "saved", "aborted-saved", "gurumeditation", "missing"}


async def get_vm_state(name):
    """Return the VBox VMState of a VM, 'missing' if unregistered, None if unknown."""
    output, ret = await sh(f"vboxmanage showvminfo {name} --machinereadable", timeout=30)
    if ret != 0:
        return "missing" if "Could not find a registered machine" in output else None
    for line in output.splitlines():
        if line.startswith("VMState="):
            return line.split("=", 1)[1].strip('"')
    return None


async def collect_vm_logs(name, logger, tail_lines=100):
    """Dump VM info and VirtualBox logs to the controller log (for failed VMs)."""
    output, ret = await sh(f"vboxmanage showvminfo {name} --machinereadable")
    logger.warning(f"collect_vm_logs: showvminfo {name} ret={ret}:\n{output}")
    log_dir = None
    for line in output.splitlines():
        if line.startswith("LogFldr="):
            log_dir = line.split("=", 1)[1].strip('"')
            break
    # VM-level logs may not exist if the VM process died very early, so also
    # collect host/driver level diagnostics
    output, ret = await sh(
        "echo '--- version'; vboxmanage --version; "
        "echo '--- /dev/vbox*'; ls -l /dev/vbox* 2>&1; "
        "echo '--- lsmod'; lsmod 2>&1 | grep -i vbox; "
        "echo '--- vboxmanage list hostonlyifs'; vboxmanage list hostonlyifs 2>&1 | head -20; "
        "echo '--- vmdir'; ls -la \"$(dirname '" + (log_dir or "/nonexistent") + "')\" 2>&1; "
        "echo '--- VBoxSVC.log'; tail -n 50 \"${VBOX_USER_HOME:-$HOME/.config/VirtualBox}/VBoxSVC.log\" 2>&1"
    )
    logger.warning(f"collect_vm_logs: host diagnostics:\n{output}")
    if not log_dir:
        logger.warning(f"collect_vm_logs: log folder not found for {name}")
        return
    output, ret = await sh(f"ls -la '{log_dir}'")
    logger.warning(f"collect_vm_logs: ls {log_dir} ret={ret}:\n{output}")
    for logname in ("VBoxStartup.log", "VBoxHardening.log", "VBox.log"):
        output, ret = await sh(f"test -f '{log_dir}/{logname}' && tail -n {tail_lines} '{log_dir}/{logname}'")
        if ret == 0:
            logger.warning(f"collect_vm_logs: {logname} (last {tail_lines} lines):\n{output}")


async def get_vm_pid(name):
    """Find the VBoxHeadless PID of a VM by exact name (None if not running).

    Uses exec (no shell) so pgrep cannot match its own parent shell, and anchors
    the pattern so that 'vbox-vm-1' does not match 'vbox-vm-10'.
    """
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", f"VBoxHeadless --comment {re.escape(name)} --startvm",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout_data, _ = await proc.communicate()
    pids = stdout_data.decode().split()
    return int(pids[0]) if pids else None


def pid_belongs_to_vm(pid, name):
    """Guard against PID reuse: check the process is still this VM's VBoxHeadless."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().decode(errors="replace").split("\0")
    except OSError:
        return False
    return "VBoxHeadless" in args[0] and name in args


class VMDeleteError(RuntimeError):
    pass


async def delete_vm(name, pid=None, logger=None):
    await sh(f"timeout 60 vboxmanage controlvm {name} poweroff")
    await asyncio.sleep(1)
    if pid is None or not pid_belongs_to_vm(pid, name):
        pid = await get_vm_pid(name)
    if pid is not None:
        if logger:
            logger.info(f"delete_vm: killing VBoxHeadless pid={pid} vm={name}")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await asyncio.sleep(1)
    # unregistervm can fail right after the kill while VBoxSVC still holds the
    # session lock, so retry a few times before giving up
    for attempt in range(3):
        output, ret = await sh(f"vboxmanage unregistervm {name} --delete-all", timeout=300)
        if ret == 0:
            return
        if "Could not find a registered machine" in output:
            return  # nothing to delete (e.g. clonevm failed before registering)
        if logger:
            logger.warning(f"delete_vm: unregistervm {name} attempt={attempt + 1} ret={ret} output={output}")
        await asyncio.sleep(2)
    raise VMDeleteError(f"Failed to unregister VM {name}: {output}")


async def create_vm(name, namespace, vboxvm_name, uid, image_name, image_tag, logger):
    logger.info(f"create_vm: Clone VM uid={uid}")
    output, ret = await sh(f"vboxmanage clonevm template-{image_name} --name={name} --register --options=link --snapshot={image_tag}", timeout=300)
    #output, ret = await sh(f"vboxmanage clonevm template-{image_name} --name={name} --register --snapshot={image_tag}")
    if ret != 0:
        raise ValueError(f"Failed to create VM {output}")

    vrdeport = 5000 + int(name.removeprefix(NAME_PFX))
    now = int(time.time())
    logger.info(f"create_vm: Modify VM uid={uid}")
    output, ret = await sh(f"vboxmanage modifyvm {name} --vrdemulticon on --vrdeport {vrdeport} --description='X-VBOX-CTL-uid={uid};X-VBOX-CTL-namespace={namespace};X-VBOX-CTL-name={vboxvm_name};X-VBOX-CTL-createdat={now}'")
    if ret != 0:
        raise ValueError(f"Failed to modify VM {output}")

    logger.info(f"create_vm: Start VM uid={uid}")
    output, ret = await sh(f"vboxmanage startvm --type headless {name}")
    if ret != 0:
        raise ValueError(f"Failed to start VM {output}")
    pid = await get_vm_pid(name)
    if pid is None:
        logger.warning(f"create_vm: could not find VBoxHeadless pid for {name}")
    logger.info(f"create_vm: Done VM uid={uid} pid={pid}")
    return pid


async def get_cr_state(namespace, name, uid, logger):
    """Check a CR against the API: 'present' (same uid), 'gone' or 'unknown'.

    Only 404 or a different uid count as 'gone'; any other failure is 'unknown'
    so an API outage or RBAC problem never causes VMs to be deleted.
    """
    sa_dir = "/var/run/secrets/kubernetes.io/serviceaccount"
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        logger.warning("reconcile: not running in-cluster, skipping CR check")
        return "unknown"
    try:
        with open(f"{sa_dir}/token") as f:
            token = f.read().strip()
        ctx = ssl.create_default_context(cafile=f"{sa_dir}/ca.crt")
        hostport = f"[{host}]" if ":" in host else host
        url = f"https://{hostport}:{port}/apis/amlight.net/v1/namespaces/{namespace}/vboxvms/{name}"
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, ssl=ctx, headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status == 404:
                    return "gone"
                if resp.status != 200:
                    logger.warning(f"reconcile: unexpected status {resp.status} for {namespace}/{name}")
                    return "unknown"
                data = await resp.json()
    except Exception as exc:
        logger.warning(f"reconcile: failed to query CR {namespace}/{name}: {exc!r}")
        return "unknown"
    return "present" if data.get("metadata", {}).get("uid") == uid else "gone"


async def rebuild_state(vm_names, logger):
    """Repopulate VMS/VMS_BY_NAME from VMs already registered in VirtualBox.

    The CR uid/namespace/name are recovered from the X-VBOX-CTL-* markers in the
    VM description. VMs with our name prefix but no readable marker are still
    reserved in VMS_BY_NAME so their slot is not handed out (and the VM not
    deleted) by a new create.
    """
    for vm_name in vm_names:
        if not vm_name.startswith(NAME_PFX):
            continue
        output, ret = await sh(f"vboxmanage showvminfo {vm_name} --machinereadable")
        if ret != 0:
            logger.warning(f"rebuild_state: failed to get info for {vm_name} ret={ret} output={output}")
            VMS_BY_NAME[vm_name] = None
            continue
        markers = {}
        for line in output.splitlines():
            if line.startswith("description="):
                markers = dict(re.findall(r"X-VBOX-CTL-(\w+)=([^;\"]*)", line))
                break
        uid = markers.get("uid")
        pid = await get_vm_pid(vm_name)
        if not uid:
            logger.warning(f"rebuild_state: no X-VBOX-CTL-uid for {vm_name}, reserving the name only")
            VMS_BY_NAME[vm_name] = None
            continue
        VMS_BY_NAME[vm_name] = uid
        VMS[uid] = {
            "body": None, "name": vm_name, "pid": pid,
            "namespace": markers.get("namespace"), "cr_name": markers.get("name"),
        }
        logger.info(f"rebuild_state: recovered vm={vm_name} uid={uid} pid={pid}")


async def reconcile_orphans(logger):
    """Delete recovered VMs whose CR no longer exists (deleted while we were down)."""
    for uid, vm in list(VMS.items()):
        if not vm.get("namespace") or not vm.get("cr_name"):
            logger.warning(f"reconcile: no namespace/name marker for vm={vm['name']}, keeping it")
            continue
        state = await get_cr_state(vm["namespace"], vm["cr_name"], uid, logger)
        if state != "gone":
            continue
        logger.warning(f"reconcile: CR {vm['namespace']}/{vm['cr_name']} uid={uid} is gone, deleting orphan vm={vm['name']}")
        async with VMS_BY_NAME_LOCK:
            try:
                await delete_vm(vm["name"], pid=vm.get("pid"), logger=logger)
            except VMDeleteError as exc:
                logger.warning(f"reconcile: {exc}. Keeping vm={vm['name']} reserved")
                continue
            VMS_BY_NAME.pop(vm["name"], None)
            VMS.pop(uid, None)


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
    logger.info('Hostonlyif vboxnet0 OK')
    # It seems that after deploying vbox hostonlyif for another user we have to
    # setup ipaddr and dhcpserver, so that the gateway option is sent to guests
    output, ret = await sh(f"vboxmanage hostonlyif ipconfig vboxnet0 --ip=192.168.56.1 --netmask=255.255.255.0")
    logger.info(f"Setup ipaddr on hostonlyif, ret={ret} result={output}")
    output, ret = await sh(f"VBoxManage dhcpserver modify --interface vboxnet0 --set-opt=3 192.168.56.1 --server-ip=192.168.56.100 --lower-ip=192.168.56.101 --upper-ip=192.168.56.254 --netmask=255.255.255.0 --enable")
    logger.info(f"Setup dhcpserver on hostonlyif, ret={ret} result={output}")
    logger.info('Checking for VM templates')
    output, ret = await sh(f"vboxmanage list vms")
    if ret != 0:
        raise ValueError(f"Failed to list VMs ret={ret} output={output}")
    registered = set(re.findall(r'^"([^"]+)"', output, re.MULTILINE))
    if templates_dir := os.environ.get("VBOXVMSCTL_TEMPLATES_DIR"):
        for vm in sorted(os.listdir(templates_dir)):
            vbox_file = os.path.join(templates_dir, vm, f"{vm}.vbox")
            if vm in registered:
                continue
            if not os.path.isfile(vbox_file):
                logger.warning(f"Template file not found, ignoring: {vbox_file}")
                continue
            reg_output, reg_ret = await sh(f"vboxmanage registervm {shlex.quote(vbox_file)}")
            if reg_ret != 0:
                logger.warning(f"Failed to register VM template {vbox_file} ret={reg_ret} output={reg_output}")
            else:
                logger.info(f"Registered VM template {vbox_file}")
    output, ret = await sh(f"vboxmanage list vms")
    if ret != 0:
        raise ValueError(f"Failed to list VMs ret={ret} output={output}")
    pattern = re.compile(r'"(?P<name>[^"]+)"\s+\{(?P<uuid>[0-9a-fA-F-]+)\}')
    vms = []
    for line in output.splitlines():
        if match := pattern.match(line):
            vms.append(match.groupdict())
    await rebuild_state([vm["name"] for vm in vms], logger)
    await reconcile_orphans(logger)
    logger.info(f"Recovered state: VMS_BY_NAME={VMS_BY_NAME}")
    pattern_snapshot = re.compile(r'\s*Name:\s+([^\s]+)\s+')
    for vm in vms:
        if vm["name"].startswith(NAME_PFX):
            continue
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
        TEMPLATES[vm["name"].removeprefix("template-")] = snapshots
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
    image = spec.get("image")
    if not isinstance(image, str) or not image:
        msg = "spec.image is required"
        patch.status["phase"] = "Failed"
        patch.status["detail"] = msg
        raise kopf.PermanentError(msg)
    image_name, _, image_tag = image.partition(":")
    image_tag = image_tag or "latest"
    if image_tag not in TEMPLATES.get(image_name, []):
        patch.status["phase"] = "Failed"
        msg = f"Image name or tag not available. Available VMs/tags: {TEMPLATES}"
        patch.status["detail"] = msg
        raise kopf.PermanentError(msg)
    async with VMS_BY_NAME_LOCK:
        if not (vm_name := await get_available_vm_name()):
            patch.status["phase"] = "Failed"
            patch.status["detail"] = "Maximum number of VBox VMs exceeded"
            raise kopf.PermanentError("Maximum number of VMs exceeded.")
        VMS_BY_NAME[vm_name] = uid
    try:
        async with VMS_BY_NAME_LOCK:
            pid = await create_vm(vm_name, namespace, name, uid, image_name, image_tag, logger)
    except Exception as exc:
        logger.info(f"Failed to create VM: {exc}. Force delete")
        async with VMS_BY_NAME_LOCK:
            try:
                await collect_vm_logs(vm_name, logger)
            except Exception as log_exc:
                logger.warning(f"Failed to collect logs for {vm_name}: {log_exc}")
            try:
                await delete_vm(vm_name, logger=logger)
            except VMDeleteError as del_exc:
                # the VM may still exist on the host, keep the slot reserved so
                # the name is not reused while it is leaked
                logger.error(f"{del_exc}. Keeping {vm_name} reserved")
            else:
                VMS_BY_NAME.pop(vm_name, None)
        raise kopf.TemporaryError("Failed to create VM. Retrying later..")
    VMS[uid] = {"body": body, "name": vm_name, "pid": pid}
    patch.status['phase'] = 'Pending'
    patch.spec["ip"] = "<none>"
    logger.info("returning status")
    return {'job1-status': 100}


@kopf.on.delete("amlight.net", "v1", "vboxvms")
async def delete(body, patch, logger, **kwargs):
    logger.info("Delete body: %s" % (body))
    uid = body["metadata"]["uid"]
    if vm := VMS.get(uid):
        async with VMS_BY_NAME_LOCK:
            try:
                await delete_vm(vm["name"], pid=vm.get("pid"), logger=logger)
            except VMDeleteError as exc:
                raise kopf.TemporaryError(str(exc), delay=15)
            VMS_BY_NAME.pop(vm["name"], None)
        VMS.pop(uid, None)
    else:
        logger.info(f"VM not found! uid={uid} VMs={VMS}")
    patch.status['phase'] = 'Succeeded'

@kopf.daemon("amlight.net", "v1", "vboxvms")
async def check_status(body, status, patch, logger, **kwargs):
    logger.info(f"Daemon for checking status body={body}...")
    uid = body["metadata"]["uid"]
    logger.info(f"Waiting for VM creation.. uid={uid}")
    for i in range(360):
        if vm := VMS.get(uid):
            break
        await asyncio.sleep(10)
    else:
        logger.info(f"Timeout waiting for VM creation! uid={uid} body={body}")
        patch.status["phase"] = "Failed"
        return

    logger.info(f"VM created! Checking status.. uid={uid}")

    start = time.time()
    while time.time() - start <= SETTINGS["max_wait"]:
        if status.get("phase", "Pending") != "Pending":
            logger.info(f"Invalid status={status} for body={body}. Aborting...")
            break
        ip = await get_vm_ip(vm["name"], logger)
        if ip:
            logger.info(f"Found IP for VM name={vm['name']} ip={ip}")
            patch.spec["ip"] = ip
            patch.status["phase"] = "Running"
            break
        await asyncio.sleep(10)
    else:
        logger.info(f"Timeout waiting for VM to be ready! start={start} now={time.time()} uid={uid} body={body}")
        patch.status["phase"] = "Failed"


@kopf.timer("amlight.net", "v1", "vboxvms", interval=30, initial_delay=60)
async def check_vm_running(body, patch, logger, **kwargs):
    """Periodically verify that VMs of Pending/Running resources are still up."""
    uid = body["metadata"]["uid"]
    phase = body.get("status", {}).get("phase")
    if phase not in ("Pending", "Running"):
        return
    if not (vm := VMS.get(uid)):
        return  # not created yet (or already deleted)
    state = await get_vm_state(vm["name"])
    if state is None:
        logger.warning(f"Could not determine state of vm={vm['name']} uid={uid}")
        return
    if state not in VM_DEAD_STATES:
        return
    logger.error(f"VM is not running! vm={vm['name']} uid={uid} state={state}")
    try:
        await collect_vm_logs(vm["name"], logger)
    except Exception as exc:
        logger.warning(f"Failed to collect logs for {vm['name']}: {exc}")
    patch.status["phase"] = "Failed"
    patch.status["detail"] = f"VM {vm['name']} is not running (state={state})"
