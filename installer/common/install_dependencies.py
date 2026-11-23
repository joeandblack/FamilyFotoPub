"""Install a verified closure without consulting a package index."""
import argparse
import subprocess
import sys
from .local_payloads import pip_arguments
from .manifest import LOCK_ROLES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=LOCK_ROLES, required=True)
    args = parser.parse_args()
    flags = pip_arguments(args.role)
    if not flags:
        parser.error('A prepared payload receipt is required')
    subprocess.run([sys.executable, '-m', 'pip', 'install', *flags], check=True)


if __name__ == '__main__':
    main()
