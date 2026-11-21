import os
import time
import requests

class VirtualeClient:
    """Fast Python client for Virtuale Android Automation API Gateway."""
    def __init__(self, base_url="http://127.0.0.1", email="admin@mkos.online", password="Ar483121"):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token = None
        self.headers = {}
        self.login()

    def login(self):
        resp = requests.post(f"{self.base_url}/api/auth/login/", json={
            "email": self.email,
            "password": self.password
        }, timeout=10)
        resp.raise_for_status()
        self.token = resp.json().get("access")
        self.headers = {"Authorization": f"Bearer {self.token}"}
        return self.token

    def get_instances(self):
        r = requests.get(f"{self.base_url}/api/instances/", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_instance_by_name(self, name):
        instances = self.get_instances()
        for inst in instances:
            if inst["name"] == name:
                return inst
        return None

    def screenshot_base64(self, instance_id):
        r = requests.get(f"{self.base_url}/api/instances/{instance_id}/screenshot/?encoding=base64", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json().get("base64")

    def screenshot_bytes(self, instance_id):
        r = requests.get(f"{self.base_url}/api/instances/{instance_id}/screenshot/", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.content

    def tap(self, instance_id, x, y):
        r = requests.post(f"{self.base_url}/api/instances/{instance_id}/tap/", json={"x": int(x), "y": int(y)}, headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def swipe(self, instance_id, x1, y1, x2, y2, duration=300):
        r = requests.post(f"{self.base_url}/api/instances/{instance_id}/swipe/", json={
            "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2), "duration": int(duration)
        }, headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def type_text(self, instance_id, text):
        r = requests.post(f"{self.base_url}/api/instances/{instance_id}/type/", json={"text": str(text)}, headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def keyevent(self, instance_id, key):
        r = requests.post(f"{self.base_url}/api/instances/{instance_id}/keyevent/", json={"key": key}, headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def ui_dump(self, instance_id):
        r = requests.get(f"{self.base_url}/api/instances/{instance_id}/ui_dump/", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json().get("xml", "")

    def agent_status(self, instance_id):
        r = requests.get(f"{self.base_url}/api/instances/{instance_id}/agent_status/", headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def inject_video(self, instance_id, server_path, filename="video.mp4"):
        r = requests.post(f"{self.base_url}/api/instances/{instance_id}/inject_video/", json={
            "path": server_path,
            "filename": filename
        }, headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def rotate_proxy(self, instance_id):
        r = requests.post(f"{self.base_url}/api/instances/{instance_id}/rotate_proxy/", headers=self.headers, timeout=20)
        r.raise_for_status()
        return r.json()
