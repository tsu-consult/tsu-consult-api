import os
import sys
import subprocess
import atexit
import time
import django
from django.db import connections
from django.db.utils import OperationalError


def start_docker_compose():
    print("🚀 Starting docker-compose services...")
    subprocess.run(["docker", "compose", "up", "-d", "postgres"], check=True)
    print("✅ All services started.")

def stop_docker_compose():
    print("🛑 Stopping docker-compose services...")
    try:
        subprocess.run(["docker", "compose", "stop"], check=True)
        print("✅ Services stopped.")
    except subprocess.CalledProcessError:
        print("⚠️ Failed to stop services.")


def wait_for_db():
    print("⏳ Waiting for database to become available...")
    for i in range(20):
        try:
            connections["default"].cursor()
            print("✅ Database is ready!")
            return
        except OperationalError:
            time.sleep(1)
    raise RuntimeError("❌ Database not ready after waiting 20 seconds.")


def init_superuser():
    from django.contrib.auth import get_user_model
    user = get_user_model()

    admin_username = os.environ.get("DJANGO_ADMIN_USERNAME", "admin")
    admin_email = os.environ.get("DJANGO_ADMIN_EMAIL", "admin@example.com")
    admin_password = os.environ.get("DJANGO_ADMIN_PASSWORD", "admin123")

    if not user.objects.filter(username=admin_username).exists():
        user.objects.create_superuser( # type: ignore[attr-defined]
            username=admin_username,
            email=admin_email,
            password=admin_password
        )
        print(f"✅ Superuser '{admin_username}' created successfully!")
    else:
        print(f"ℹ️ Superuser '{admin_username}' already exists.")

def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    DEBUG = os.environ.get("DEBUG", "True").lower() in ["1", "true", "yes"]
    runserver_related = len(sys.argv) > 1 and sys.argv[1] in ["runserver", "migrate", "shell"]

    try:
        from django.core.management import execute_from_command_line, call_command
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Make sure it's installed and available on your PYTHONPATH."
        ) from exc

    print("💾 Setting up Django...")
    django.setup()

    if runserver_related and DEBUG:
        start_docker_compose()
        atexit.register(stop_docker_compose)
        wait_for_db()

    print("💾 Applying migrations automatically...")
    call_command("migrate", interactive=False)

    print("💻 Checking/creating superuser...")
    init_superuser()

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
