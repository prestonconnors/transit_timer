# Gunicorn configuration file: gunicorn_config.py
bind = "127.0.0.1:10929"  # Bind to localhost on port 10929
workers = 1  # Number of worker processes to handle requests
threads = 2  # Number of threads per worker
worker_class = "gthread"  # Use the gthread worker for thread-based concurrency
accesslog = "-"  # Log access to stdout
errorlog = "-"  # Log errors to stdout
loglevel = "warning"
keepalive = 120  # Keep connections alive for 120 seconds