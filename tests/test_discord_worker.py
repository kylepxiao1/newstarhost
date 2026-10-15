"""Exercise the real worker with inert child commands; never contact services."""
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
VERSION = subprocess.check_output([BASH, '-c', 'echo ${BASH_VERSINFO[0]}'], text=True).strip() if BASH else '0'


@unittest.skipUnless(int(VERSION) >= 4, 'Worker requires Bash 4+ for wait -n (production uses Linux)')
class DiscordWorkerTests(unittest.TestCase):
    def run_worker(self, enabled):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'child.py'
            fixture.write_text('''import os,sys,signal,time
from pathlib import Path
root=Path(sys.argv[1]); role=sys.argv[2]
with (root/(role+'.starts')).open('a') as f: f.write(str(os.getpid())+'\\n')
if role=='trend': sys.exit(0)
if role=='summary' and len((root/'summary.starts').read_text().splitlines()) < 3: sys.exit(7)
def stop(*args):
 (root/(role+'.stopped')).write_text('yes')
 sys.exit(0)
signal.signal(signal.SIGTERM,stop)
while True: time.sleep(.05)
''')
            def command(role):
                return shlex.join([sys.executable, str(fixture), directory, role])
            env = dict(os.environ)
            env.update({
                'DISCORD_WORKER_ENV_FILE': '/dev/null', 'TRENDBOT_ENV_FILE': '/dev/null',
                'TRENDBOT_RUN_CMD': command('trend'), 'TRENDBOT_INTERVAL': '600',
                'DISCORD_VERIFY_BOT_CMD': command('verify'),
                'LISTENER_HEARTBEAT_WATCHDOG_CMD': command('watchdog'),
                'LISTENER_HEARTBEAT_WATCHDOG_ENABLED': '1',
                'WILDCARDZ_CALENDAR_ID': 'fixture', 'WILDCARDZ_CALENDAR_API_KEY': 'fixture',
                'WILDCARDZ_REMINDER_CMD': command('reminder'), 'WILDCARDZ_REMINDER_ENABLED': '1',
                'WILDCARDZ_TOPGIFTER_CMD': command('topgifter'),
                'WILDCARDZ_TOPGIFTER_ENABLED': '1', 'WILDCARDZ_TOPGIFTER_WEBHOOK': 'fixture',
                'GIFTER_SUMMARY_ENABLED': str(int(enabled)),
                'GIFTER_SUMMARY_CMD': command('summary'), 'GIFTER_SUMMARY_RESTART_SECONDS': '0.1',
            })
            output = root / 'worker.log'
            with output.open('w') as log:
                worker = subprocess.Popen([BASH, str(ROOT/'scripts/discord_worker.sh')],
                    env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    deadline = time.monotonic()+12
                    roles = ['trend', 'verify', 'watchdog', 'reminder', 'topgifter']
                    while time.monotonic() < deadline:
                        ready = all((root/(role+'.starts')).exists() for role in roles)
                        summary = root/'summary.starts'
                        if ready and (not enabled or (summary.exists() and len(summary.read_text().splitlines()) >= 3)):
                            break
                        if worker.poll() is not None:
                            self.fail(output.read_text())
                        time.sleep(.05)
                    else:
                        self.fail('Startup timed out: '+output.read_text())
                    self.assertIsNone(worker.poll(), output.read_text())
                    for role in roles:
                        self.assertEqual(len((root/(role+'.starts')).read_text().splitlines()), 1, role)
                    if not enabled:
                        self.assertFalse(summary.exists())
                    worker.send_signal(signal.SIGTERM)
                    worker.wait(timeout=10)
                    self.assertEqual(worker.returncode, 0, output.read_text())
                    if enabled:
                        self.assertTrue((root/'summary.stopped').exists(), output.read_text())
                        self.assertIn('status=7', output.read_text())
                finally:
                    try:
                        os.killpg(worker.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    worker.wait(timeout=5)

    def test_summary_restarts_without_restarting_existing_tasks_and_receives_shutdown(self):
        self.run_worker(True)

    def test_disabling_summary_preserves_existing_tasks(self):
        self.run_worker(False)


if __name__ == '__main__':
    unittest.main()
