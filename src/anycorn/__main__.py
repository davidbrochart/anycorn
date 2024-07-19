from __future__ import annotations

import ssl
import sys

import rich_click as click

from .config import Config
from .run import run


def _load_config(config_path: str | None) -> Config:
    if config_path is None:
        return Config()
    elif config_path.startswith("python:"):
        return Config.from_object(config_path[len("python:") :])
    elif config_path.startswith("file:"):
        return Config.from_pyfile(config_path[len("file:") :])
    else:
        return Config.from_toml(config_path)


@click.command(
    help="Start the server and dispatch to the APPLICATION as path.to.module:instance.path."
)
@click.argument(
    "application",
)
@click.option(
    "--access-logfile",
    help="The target location for the access log, use `-` for stdout",
)
@click.option(
    "--access-logformat",
    help="The log format for the access log, see help docs",
)
@click.option("--backlog", type=int, help="The maximum number of pending connections")
@click.option(
    "-b",
    "--bind",
    "binds",
    help="The TCP host/address to bind to. Should be either host:port, host, "
    "unix:path or fd://num, e.g. 127.0.0.1:5000, 127.0.0.1, "
    "unix:/tmp/socket or fd://33 respectively.",
    default=[],
    multiple=True,
)
@click.option(
    "--ca-certs",
    help="Path to the SSL CA certificate file",
)
@click.option(
    "--certfile",
    help="Path to the SSL certificate file",
)
@click.option(
    "--cert-reqs",
    type=int,
    help="See verify mode argument",
)
@click.option(
    "--ciphers",
    help="Ciphers to use for the SSL setup",
)
@click.option(
    "-c",
    "--config",
    help="Location of a TOML config file, or when prefixed with `file:` a Python file, "
    "or when prefixed with `python:` a Python module.",
)
@click.option(
    "--debug",
    help="Enable debug mode, i.e. extra logging and checks",
    is_flag=True,
)
@click.option(
    "--error-logfile",
    "--log-file",
    "error_logfile",
    help="The target location for the error log, use `-` for stderr",
)
@click.option(
    "--graceful-timeout",
    help="Time to wait after SIGTERM or Ctrl-C for any remaining requests (tasks) to complete.",
    type=int,
)
@click.option(
    "--read-timeout",
    help="Seconds to wait before timing out reads on TCP sockets",
    type=int,
)
@click.option(
    "--max-requests",
    help="Maximum number of requests a worker will process before restarting",
    type=int,
)
@click.option(
    "--max-requests-jitter",
    help="This jitter causes the max-requests per worker to be "
    "randomized by randint(0, max_requests_jitter)",
    type=int,
)
@click.option(
    "-g",
    "--group",
    help="Group to own any unix sockets.",
    type=int,
)
@click.option(
    "-k",
    "--worker-class",
    help="The type of worker to use. Options include asyncio and trio.",
    type=click.Choice(("asyncio", "trio")),
)
@click.option(
    "--keep-alive",
    help="Seconds to keep inactive connections alive for",
    type=int,
)
@click.option(
    "--keyfile",
    help="Path to the SSL key file",
)
@click.option(
    "--keyfile-password",
    help="Password to decrypt the SSL key file",
)
@click.option(
    "--insecure-bind",
    "insecure_binds",
    help="The TCP host/address to bind to. SSL options will not apply to these binds. "
    "See *bind* for formatting options. Care must be taken! See HTTP -> HTTPS redirection docs.",
    default=[],
    multiple=True,
)
@click.option(
    "--log-config",
    help="A Python logging configuration file. This can be prefixed with "
    "'json:' or 'toml:' to load the configuration from a file in "
    " that format. Default is the logging ini format.",
)
@click.option(
    "--log-level",
    help="The (error) log level, defaults to info",
)
@click.option(
    "-p",
    "--pid",
    help="Location to write the PID (Program ID) to.",
)
# FIXME
# parser.add_argument(
#     "--quic-bind",
#     dest="quic_binds",
#     help="""The UDP/QUIC host/address to bind to. See *bind* for formatting
#     options.
#     """,
#     default=[],
#     action="append",
# )
@click.option(
    "--reload",
    help="Enable automatic reloads on code changes",
    is_flag=True,
)
@click.option(
    "--root-path",
    help="The setting for the ASGI root_path variable",
)
@click.option(
    "--server-name",
    "server_names",
    help="The hostnames that can be served, requests to different hosts "
    "will be responded to with 404s.",
    default=[],
    multiple=True,
)
@click.option(
    "--statsd-host",
    help="The host:port of the statsd server",
)
@click.option(
    "--statsd-prefix",
    help="Prefix for all statsd messages",
    default="",
)
@click.option(
    "-m",
    "--umask",
    help="The permissions bit mask to use on any unix sockets.",
    type=int,
)
@click.option(
    "-u",
    "--user",
    help="User to own any unix sockets.",
    type=int,
)
@click.option(
    "--verify-mode",
    help="SSL verify mode for peer's certificate, see ssl.VerifyMode enum for possible values.",
    type=click.Choice(("CERT_NONE", "CERT_OPTIONAL", "CERT_REQUIRED")),
)
@click.option(
    "--websocket-ping-interval",
    help="If set this is the time in seconds between pings sent to the client. "
    "This can be used to keep the websocket connection alive.",
    type=int,
)
@click.option(
    "-w",
    "--workers",
    help="The number of workers to spawn and use",
    type=int,
)
def main(
    application: str,
    access_logfile: str | None,
    access_logformat: str | None,
    backlog: int | None,
    binds: list[str],
    ca_certs: str | None,
    certfile: str | None,
    cert_reqs: int | None,
    ciphers: str | None,
    config: str | None,
    debug: bool,
    error_logfile: str | None,
    graceful_timeout: int | None,
    read_timeout: int | None,
    max_requests: int | None,
    max_requests_jitter: int | None,
    group: int | None,
    worker_class: str | None,
    keep_alive: int | None,
    keyfile: str | None,
    keyfile_password: str | None,
    insecure_binds: list[str],
    log_config: str | None,
    log_level: str | None,
    pid: str | None,
    reload: bool,
    root_path: str | None,
    server_names: list[str],
    statsd_host: str | None,
    statsd_prefix: str,
    umask: int | None,
    user: int | None,
    verify_mode: str | None,
    websocket_ping_interval: int | None,
    workers: int | None,
) -> int:
    config = _load_config(config)
    config.application_path = application

    if log_level is not None:
        config.loglevel = log_level
    if access_logformat is not None:
        config.access_log_format = access_logformat
    if access_logfile is not None:
        config.accesslog = access_logfile
    if backlog is not None:
        config.backlog = backlog
    if ca_certs is not None:
        config.ca_certs = ca_certs
    if certfile is not None:
        config.certfile = certfile
    if cert_reqs is not None:
        config.cert_reqs = cert_reqs
    if ciphers is not None:
        config.ciphers = ciphers
    if debug is not None:
        config.debug = debug
    if error_logfile is not None:
        config.errorlog = error_logfile
    if graceful_timeout is not None:
        config.graceful_timeout = graceful_timeout
    if read_timeout is not None:
        config.read_timeout = read_timeout
    if group is not None:
        config.group = group
    if keep_alive is not None:
        config.keep_alive_timeout = keep_alive
    if keyfile is not None:
        config.keyfile = keyfile
    if keyfile_password is not None:
        config.keyfile_password = keyfile_password
    if log_config is not None:
        config.logconfig = log_config
    if max_requests is not None:
        config.max_requests = max_requests
    if max_requests_jitter is not None:
        config.max_requests_jitter = max_requests
    if pid is not None:
        config.pid_path = pid
    if root_path is not None:
        config.root_path = root_path
    if reload is not None:
        config.use_reloader = reload
    if statsd_host is not None:
        config.statsd_host = statsd_host
    if statsd_prefix is not None:
        config.statsd_prefix = statsd_prefix
    if umask is not None:
        config.umask = umask
    if user is not None:
        config.user = user
    if worker_class is not None:
        config.worker_class = worker_class
    if verify_mode is not None:
        config.verify_mode = ssl.VerifyMode[verify_mode]
    if websocket_ping_interval is not None:
        config.websocket_ping_interval = websocket_ping_interval
    if workers is not None:
        config.workers = workers

    if len(binds) > 0:
        config.bind = binds
    if len(insecure_binds) > 0:
        config.insecure_bind = insecure_binds
    # FIXME
    # if len(args.quic_binds) > 0:
    #     config.quic_bind = args.quic_binds
    if len(server_names) > 0:
        config.server_names = server_names

    return run(config)


if __name__ == "__main__":
    sys.exit(main())
