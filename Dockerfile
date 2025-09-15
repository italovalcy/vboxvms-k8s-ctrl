FROM python:3.11

WORKDIR /app
COPY vbox-vms-ctrl.py .

RUN --mount=source=.,target=/mnt,type=bind \
    export DEBIAN_FRONTEND=noninteractive \
 && pip install --no-cache-dir -r requirements.txt

CMD ["python3", "vbox-vms-ctrl.py"]
