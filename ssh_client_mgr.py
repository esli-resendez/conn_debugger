import paramiko
import sys
import time
import socket
import re
from datetime import datetime

class SSHClientWrapper:
    def __init__(self, host="localhost", port=22, username="root", password="msft", logger=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.logger = logger
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def connect(self):
        try:
            self.client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
                banner_timeout=10,
                auth_timeout=10
            )
            print(f"[+] Connected to {self.host}:{self.port}")
        except (paramiko.SSHException, socket.error, socket.timeout) as e:
            if self.logger:
                self.logger.log("CONNECTION", f"ERROR: {str(e)}")
                self.logger.close()
            print(f"[!] Connection failed: {e}")
            sys.exit(1)

    def write(self, command):
        try:
            self.client.exec_command(command)
        except Exception as e:
            if self.logger:
                self.logger.log(command, f"WRITE ERROR: {str(e)}")

    def read(self, stdout, stderr):
        output = stdout.read().decode(errors="ignore")
        error = stderr.read().decode(errors="ignore")
        return output + error

    def query(self, command, timeout=60):
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            result = self.read(stdout, stderr)
            if self.logger:
                self.logger.log(command, result)
            return result
        except Exception as e:
            if self.logger:
                self.logger.log(command, f"QUERY ERROR: {str(e)}")
            return ""

    def close(self):
        self.client.close()