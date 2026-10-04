"""Offline guest executable entrypoint, Windows service and target worker."""

from __future__ import annotations

import os
import sys
import threading


def main(argv=None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--initialize-target-user"]:
        if os.name != "nt":
            raise RuntimeError("target specialization requires Windows")
        from argus.capsule.bootstrap_service import prepare_bootstrap_service
        from argus.capsule.windows_target import initialize_target_user

        prepared = prepare_bootstrap_service()
        try:
            initialize_target_user(prepared.manifest.capsule_id,
                                   prepared.control_state_store.path.parent)
        finally:
            prepared.cleanup_all_staging()
        return
    if args == ["--target-worker"]:
        from argus.capsule.target_worker import worker_main

        worker_main()
        return
    if os.name == "nt" and "--bootstrap-service" in args:
        import servicemanager
        import win32service
        import win32serviceutil

        class BootstrapService(win32serviceutil.ServiceFramework):
            _svc_name_ = "ArgusBootstrap"
            _svc_display_name_ = "Argus Capsule Bootstrap"

            def __init__(self, service_args):
                super().__init__(service_args)
                self.stop_event = threading.Event()

            def SvcStop(self):
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
                self.stop_event.set()

            def SvcDoRun(self):
                from argus.capsule.secure_guest_agent import main as agent_main

                agent_main(args, stop_event=self.stop_event)

        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(BootstrapService)
        servicemanager.StartServiceCtrlDispatcher()
        return

    from argus.capsule.secure_guest_agent import main as agent_main

    agent_main(args)


if __name__ == "__main__":
    main()
