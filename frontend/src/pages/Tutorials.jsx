import { useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { startTour } from "@/components/GuidedTour";
import { TOURS } from "@/lib/tours";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { GraduationCap, PlayCircle, MousePointerClick } from "lucide-react";

const API = process.env.REACT_APP_BACKEND_URL;
const TRACKS = [
    { id: "retail", label: "Traders & Investors" },
    { id: "manager", label: "PAMM Managers" },
];
const TOUR_FOR = {
    "getting-started": "getting-started",
    "ai-trading-bot": "ai-trading-bot",
    "pamm-manager": "pamm-manager",
    "investor-flow": "investor-flow",
};

function VideoCard({ t }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid={`tutorial-card-${t.slug}`}>
            <video controls preload="metadata" className="w-full aspect-video bg-black"
                data-testid={`tutorial-video-${t.slug}`}
                src={`${API}/api/tutorials/media/${t.file}`} />
            <div className="p-4 space-y-2 font-mono">
                <div className="flex items-center justify-between">
                    <span className="text-sm font-bold text-[#E4E4E7]">{t.title}</span>
                    <span className="text-[10px] text-[#52525B]">{Math.round(t.duration_s)}s · AI narrated</span>
                </div>
                <p className="text-xs text-[#A1A1AA] leading-relaxed">{t.description}</p>
                {TOUR_FOR[t.slug] && TOURS[TOUR_FOR[t.slug]] && (
                    <button onClick={() => startTour(TOUR_FOR[t.slug])}
                        data-testid={`tour-start-${t.slug}`}
                        className="flex items-center gap-1.5 border border-[#1F1F1F] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[#A1A1AA] hover:border-[#00FF41] hover:text-[#00FF41] transition-colors">
                        <MousePointerClick className="h-3.5 w-3.5" /> Interactive tour
                    </button>
                )}
            </div>
        </div>
    );
}

export default function Tutorials() {
    const [manifest, setManifest] = useState(null);
    const [track, setTrack] = useState("retail");

    useEffect(() => {
        api.get("/tutorials/manifest")
            .then(r => setManifest(r.data))
            .catch(e => toast.error(formatApiError(e)));
    }, []);

    const vids = (manifest?.tutorials || []).filter(t => t.track === track);

    return (
        <AppLayout>
            <PageHeader title="Tutorials" testid="tutorials-header"
                subtitle="AI-narrated video walkthroughs + hands-on interactive tours for new users" />
            <div className="px-4 md:px-8 py-6 space-y-6" data-testid="tutorials-page">
                <div className="flex gap-2 font-mono" data-testid="tutorials-track-tabs">
                    {TRACKS.map(tr => (
                        <button key={tr.id} onClick={() => setTrack(tr.id)}
                            data-testid={`tutorials-track-${tr.id}`}
                            className={`border px-4 py-2 text-xs uppercase tracking-wider transition-colors ${track === tr.id ? "border-[#00FF41] text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#3F3F46]"}`}>
                            {tr.label}
                        </button>
                    ))}
                </div>
                {!manifest && (
                    <div className="font-mono text-xs text-[#52525B]">Loading tutorials…</div>
                )}
                {manifest && vids.length === 0 && (
                    <div className="border border-[#1F1F1F] p-6 font-mono text-xs text-[#52525B] flex items-center gap-2" data-testid="tutorials-empty">
                        <GraduationCap className="h-4 w-4" /> No tutorials for this track yet.
                    </div>
                )}
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    {vids.map(t => <VideoCard key={t.slug} t={t} />)}
                </div>
                <div className="border border-[#1F1F1F] p-4 font-mono text-[11px] text-[#52525B] flex items-center gap-2">
                    <PlayCircle className="h-4 w-4 text-[#00FF41]" />
                    Videos are generated from the real app with an AI voiceover. Interactive tours highlight the actual buttons on your screen — click Next to walk through.
                </div>
            </div>
        </AppLayout>
    );
}
