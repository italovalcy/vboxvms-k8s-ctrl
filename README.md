# vboxvms-k8s-ctrl

This repo includes a Kubernetes controller/operator to add support for VirtualBox VMs into Kubernetes. The VBox VMs will run on Nodes that have the VirtualBox installation.


## Pre-requirements

- The kubernetes nodes that will be elegible to run VBox VMs have to have VirtualBox Installation and Kernel modules properly loaded. Virtualbox kernel module conflicts with KVM, so you have to choose one of them to run.

- You must provide VirtualBox "template" images and optionally "snapshots" for each VM (if you want to allow users run them on specific versions). Template VMs are the ones available at the environment variable `VBOXVMSCTL_TEMPLATES_DIR` (check the file `VBOXVMSCTL_TEMPLATES_DIR`). If no snapshots are provided, one default snapshot will be created and named "latest".


## Setup

As kubernetes admin:

```
kubectl --kubeconfig ~/.kube/config-admin apply -f crd.yaml
kubectl --kubeconfig ~/.kube/config-admin apply -f vboxvms-deployment.yaml
```

Next you have to adjust your RBAC permissions for your users to allow creation of VBoxVMs resources. This has to be done **for each user** on **each namespace** you want to allow access to VBoxVMs resource! Here is an example (please adjust your service account name and namespace accordingly -- here we use service account as `ns-admin` and namespace as `amlight`):
```
sed -e "s/XXXSERVICEACCOUNTXXX/ns-admin/g; s/XXXNAMESPACEXXX/amlight/g" rbac.yaml | kubectl --kubeconfig ~/.kube/config-admin apply -f -
```

Now you can create your resources:
```
kubectl apply -f test-vboxvms.yaml
```

## Important considerations

### Source NAT for Pod network

When using Kubernetes with Calico as the Container Network Interface (CNI) plugin, please be advised that by default Calico applies Source NAT for IP address leaving the cluster network context. In practice, that means Pod IP addresses will be translated into the nearest IP address of the VirtualBox's internal network used for VMs -- usually `vboxnet0` -- for instance `192.168.56.1`, which makes it difficult to apply VXLAN/L2TP tunnels and access control rules on the VMs. In order to configure Calico to not apply NAT to certain networks such as the virtualbox internal network, you can apply the following config to your Kubernetes cluster:

```
cat >config-calico-no-nat-192.168.56.0.yaml <<<EOF
apiVersion: projectcalico.org/v3
kind: IPPool
metadata:
  name: no-nat-192.168.56.0-24
spec:
  cidr: 192.168.56.0/24
  disabled: true
  natOutgoing: false
EOF

kubectl --kubeconfig ~/.kube/config-admin -n calico-system apply -f config-calico-no-nat-192.168.56.0.yaml
```

### VirtualBox configs

DHCPServer:
```
vboxmanage list dhcpservers

# if you dont find vboxnet0:
VBoxManage hostonlyif create
VBoxManage dhcpserver modify --network HostInterfaceNetworking-vboxnet0 --set-opt=3 192.168.56.1
vboxmanage dhcpserver restart --network HostInterfaceNetworking-vboxnet0
```

After import VM:
```
vboxmanage modifyvm VMNAME --vrdemulticon on --vrdeport 5015 --vrde on --memory 10000 --cpus 4 --vrde-auth-type null
vboxmanage modifyvm VMNAME --nic1 hostnet --hostnet1 vboxnet0
```

You can download a pre-built virtualbox image from OSBoxes or VagrantCloud projects (or any other of your choice):
- Example for OSBoxes: https://www.osboxes.org/debian/#debian-12-11-0-vbox
- Example for VagrantCloud: https://portal.cloud.hashicorp.com/vagrant/discover/generic/debian12

If you are importing a vdi/vmdk:
```
VBoxManage createvm --name template-debian12 --ostype Debian_64 --register
VBoxManage storagectl template-debian12 --name "SATA Controller" --add sata --controller IntelAhci
mv debian12-disk.vmdk VirtualBox\ VMs/template-debian12/
cd VirtualBox\ VMs/template-debian12/
VBoxManage storageattach template-debian12 --storagectl "SATA Controller" --port 0 --device 0 --type hdd --medium debian12-disk.vmdk
VBoxManage modifyvm template-debian12 --memory 2048 --vram 128
VBoxManage modifyvm template-debian12 --nic1 hostnet --hostnet1 vboxnet0
vboxmanage modifyvm template-debian12 --vrdemulticon on --vrdeport 5015 --vrde on --memory 10000 --cpus 4 --vrde-auth-type null
```
