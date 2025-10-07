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
