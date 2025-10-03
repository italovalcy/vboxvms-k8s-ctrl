FROM python:3.11-bookworm

RUN export DEBIAN_FRONTEND=noninteractive \
 && apt-get update && apt-get install -y curl gpg fasttrack-archive-keyring iproute2 sudo \
 && echo "deb https://fasttrack.debian.net/debian-fasttrack/ bookworm-fasttrack main contrib" | tee /etc/apt/sources.list.d/fasttrack.list \
 && echo "deb https://fasttrack.debian.net/debian-fasttrack/ bookworm-backports-staging main contrib" | tee -a /etc/apt/sources.list.d/fasttrack.list \
 && rm -rf /var/lib/apt/lists/*
# && curl -L https://www.virtualbox.org/download/oracle_vbox_2016.asc \
#     | gpg --yes --output /usr/share/keyrings/oracle-virtualbox-2016.gpg --dearmor \
# && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/oracle-virtualbox-2016.gpg] https://download.virtualbox.org/virtualbox/debian bookworm contrib" \
#      | tee -a /etc/apt/sources.list.d/virtualbox.list \

RUN export DEBIAN_FRONTEND=noninteractive \
 && echo virtualbox-ext-pack virtualbox-ext-pack/license select true | debconf-set-selections \
 && apt-get update && apt-get install -y --no-install-recommends kmod virtualbox virtualbox-ext-pack \
 && rm -rf /var/lib/apt/lists/*

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
