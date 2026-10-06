FROM python:3.11-bookworm

# The VirtualBox userspace version MUST match the vboxdrv kernel module loaded
# on the host (see /sys/module/vboxdrv/version), otherwise VBoxHeadless fails
# with rc=-1912 (driver version mismatch). Keep these in sync with the host.
ARG VBOX_VERSION=7.1.18
ARG VBOX_BUILD=173720

RUN export DEBIAN_FRONTEND=noninteractive \
 && apt-get update && apt-get install -y curl gpg iproute2 sudo kmod \
 && curl -fsSL https://www.virtualbox.org/download/oracle_vbox_2016.asc \
     | gpg --yes --output /usr/share/keyrings/oracle-virtualbox-2016.gpg --dearmor \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/oracle-virtualbox-2016.gpg] https://download.virtualbox.org/virtualbox/debian bookworm contrib" \
     > /etc/apt/sources.list.d/virtualbox.list \
 && rm -rf /var/lib/apt/lists/*

RUN export DEBIAN_FRONTEND=noninteractive \
 && apt-get update \
 && apt-cache policy virtualbox-7.1 \
 && apt-get install -y --no-install-recommends \
     virtualbox-7.1=${VBOX_VERSION}-${VBOX_BUILD}~Debian~bookworm \
 && rm -rf /var/lib/apt/lists/*

# NOTE: the extpack file must keep its original name; VBoxManage derives the
# pack name from it and rejects e.g. "extpack.vbox-extpack"
RUN set -eux; \
    ep=/tmp/Oracle_VirtualBox_Extension_Pack-${VBOX_VERSION}.vbox-extpack; \
    curl -fsSL -o "$ep" \
      https://download.virtualbox.org/virtualbox/${VBOX_VERSION}/Oracle_VirtualBox_Extension_Pack-${VBOX_VERSION}.vbox-extpack; \
    lic=$(tar -xzOf "$ep" --wildcards '*ExtPack-license.txt' | sha256sum | cut -d' ' -f1); \
    echo "extpack license sha256=$lic"; \
    VBoxManage extpack install --replace --accept-license="$lic" "$ep"; \
    rm -f "$ep"

RUN groupadd --gid 1000 vboxvmsctl \
 && useradd -r -g vboxvmsctl -G vboxusers --uid 1000 --home-dir /app --create-home vboxvmsctl \
 && chown -R vboxvmsctl:vboxvmsctl /app \
 && echo "vboxvmsctl ALL=(ALL) NOPASSWD: ALL" | tee /etc/sudoers.d/vboxvmsctl

RUN --mount=source=./requirements.txt,target=/mnt/requirements.txt,type=bind \
    export DEBIAN_FRONTEND=noninteractive \
 && pip install --no-cache-dir -r /mnt/requirements.txt

WORKDIR /app
USER vboxvmsctl

COPY vbox-vms-ctrl.py .


ENTRYPOINT ["kopf", "run", "--all-namespaces", "vbox-vms-ctrl.py"]
