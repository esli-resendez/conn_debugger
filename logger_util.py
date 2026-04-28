from datetime import datetime

class Logger:
    def __init__(self, task_id, node_pos=4):
        timestamp = datetime.now().strftime("%y%m%d%H%M%S")
        self.filename = f"output/ssh_task_{task_id}_node_{node_pos}_{timestamp}.log"
        self.file = open(self.filename, "a", encoding="utf-8")

    def log(self, command, output):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.file.write(f"[{ts}] COMMAND: {command}\n")
        self.file.write(f"[{ts}] OUTPUT:\n{output}\n")
        self.file.write("=" * 60 + "\n")
        self.file.flush()

    def close(self):
        if not self.file.closed:
            self.file.close()