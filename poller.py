"""
Generic Oracle Cloud Always-Free ARM Capacity Poller (Zero Hardcoded Secrets/PII)
All configuration is injected securely at runtime from encrypted environment secrets.
"""
import os
import time
import random
import base64
import requests
import oci

ANTI_IDLE_INIT = """#!/bin/bash
set -e
apt-get update -y && apt-get install -y python3
cat << 'EOF' > /usr/local/bin/oracle_anti_idle.py
import time, os, threading
def cpu_w():
    while True:
        s = time.time()
        while (time.time() - s) < 0.022:
            _ = 999999 * 999999
        time.sleep(0.078)
def mem_w():
    try:
        ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        buf = bytearray(int(ram * 0.22))
        while True:
            for i in range(0, len(buf), 67108864):
                buf[i] = (buf[i] + 1) % 255
            time.sleep(60)
    except Exception:
        pass
if __name__ == '__main__':
    threading.Thread(target=cpu_w, daemon=True).start()
    threading.Thread(target=mem_w, daemon=True).start()
    while True:
        time.sleep(3600)
EOF
cat << 'EOF' > /etc/systemd/system/oracle-anti-idle.service
[Unit]
Description=Oracle Always-Free Anti-Idle Baseline (22%)
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/oracle_anti_idle.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now oracle-anti-idle.service
"""

def notify(title: str, body: str, priority: str = "default"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    email = os.environ.get("NOTIFY_EMAIL", "").strip()
    if not topic:
        return
    headers = {"Title": title, "Priority": priority}
    if email and "@" in email:
        headers["Email"] = email
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=10)
    except Exception:
        pass

def main():
    cfg = oci.config.from_file(os.path.expanduser("~/.oci/config"), "DEFAULT")
    tenancy = cfg["tenancy"]
    identity = oci.identity.IdentityClient(cfg, timeout=(15.0, 60.0))
    compute = oci.core.ComputeClient(cfg, timeout=(15.0, 60.0))
    network = oci.core.VirtualNetworkClient(cfg, timeout=(15.0, 60.0))

    # Check if an active instance already exists
    for inst in compute.list_instances(tenancy).data:
        if inst.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING"):
            print("[OK] Active ARM instance already exists.", flush=True)
            open("/tmp/provisioned.done", "w").write("DONE")
            return

    ads = [ad.name for ad in identity.list_availability_domains(tenancy).data]
    vcns = [v for v in network.list_vcns(tenancy).data if v.lifecycle_state == "AVAILABLE"]
    subnets = [s for s in network.list_subnets(tenancy, vcn_id=vcns[0].id).data if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic]
    images = compute.list_images(tenancy, operating_system="Canonical Ubuntu", shape="VM.Standard.A1.Flex", sort_by="TIMECREATED", sort_order="DESC").data
    img = next((i for i in images if "24.04" in i.display_name.lower() and "aarch64" in i.display_name.lower()), images[0])
    ssh_pub = open(os.path.expanduser("~/.ssh/id_oci.pub"), "r", encoding="utf-8").read().strip()

    launch_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=tenancy,
        availability_domain=ads[0],
        display_name="oracle-maskcode-studio",
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2.0, memory_in_gbs=12.0),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=img.id, boot_volume_size_in_gbs=100),
        create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subnets[0].id, assign_public_ip=True),
        metadata={"ssh_authorized_keys": ssh_pub, "user_data": base64.b64encode(ANTI_IDLE_INIT.encode()).decode()},
    )

    start_ts = time.time()
    attempt = 1
    while (time.time() - start_ts) < 18000:
        print(f"[Attempt {attempt}] Polling free ARM capacity...", flush=True)
        try:
            inst = compute.launch_instance(launch_details).data
            oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING")
            vas = compute.list_vnic_attachments(tenancy, instance_id=inst.id).data
            pub_ip = next((network.get_vnic(v.vnic_id).data.public_ip for v in vas if v.lifecycle_state == "ATTACHED" and network.get_vnic(v.vnic_id).data.public_ip), "unknown")
            # Do NOT print public IP to public logs; send strictly to private encrypted ntfy/email channel
            notify("Oracle Cloud ARM Provisioned!", f"Always-Free ARM instance is RUNNING!\nPublic IP: {pub_ip}\nAnti-Idle 22% Watchdog: Active", priority="urgent")
            open("/tmp/provisioned.done", "w").write("DONE")
            print("[SUCCESS] Instance provisioned and private alert sent.", flush=True)
            return
        except oci.exceptions.ServiceError as e:
            wait = random.randint(95, 130)
            print(f"[Retry] Status {e.status}. Quiet wait {wait}s...", flush=True)
            time.sleep(wait)
        except Exception:
            wait = random.randint(95, 130)
            print(f"[Retry] Transient error. Quiet wait {wait}s...", flush=True)
            time.sleep(wait)
        if attempt % 1000 == 0:
            notify(f"Poller Milestone: Attempt {attempt}", f"Reached {attempt} quiet polling attempts. Still active.")
        attempt += 1

    print("[Relay] 5-hour cycle complete; handing off to next runner.", flush=True)

if __name__ == "__main__":
    main()
