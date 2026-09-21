import { Terminal, ShieldAlert } from "lucide-react";

export function NotEnabled() {
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-6 max-w-3xl" data-testid="hm-not-enabled">
            <div className="flex items-center gap-2 mb-3">
                <ShieldAlert className="w-4 h-4 text-[#FFB020]" />
                <span className="font-mono text-[10px] tracking-widest text-[#FFB020]">MIGRATION SIDECAR NOT ENABLED</span>
            </div>
            <p className="text-sm text-[#A1A1AA] mb-4">
                Moving hosts needs a privileged worker (docker socket + SSH) that the API container deliberately does
                not have. Enable the sidecar on the <b className="text-[#E4E4E7]">current</b> server, then reload this page.
                Remove it again after the migration.
            </p>
            <pre className="bg-[#050505] border border-[#1F1F1F] p-3 text-xs font-mono text-[#00FF41] overflow-auto" data-testid="hm-enable-cmd">
{`sudo -iu stoic && cd ~/stoic
make migrator-on        # builds docker-compose.migrator.yml, restarts the API with MIGRATOR_URL
# … run the wizard …
make migrator-off       # remove the sidecar when done`}
            </pre>
            <div className="mt-4 text-xs text-[#71717A] flex items-start gap-2">
                <Terminal className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                <span>The sidecar is reachable only inside the compose network and authenticates the API with the shared
                    METRICS_TOKEN. Destructive steps (freeze, decommission, abort) additionally require a fresh 2FA step-up here.</span>
            </div>
        </div>
    );
}

export const STATUS_CLS = {
    pending: "text-[#52525B] border-[#1F1F1F]",
    running: "text-[#FFB020] border-[#FFB020]/50",
    done: "text-[#00FF41] border-[#00FF41]/50",
    failed: "text-[#FF3B30] border-[#FF3B30]/50",
};

export const fmtBytes = b => (b == null ? "—" : b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`);
