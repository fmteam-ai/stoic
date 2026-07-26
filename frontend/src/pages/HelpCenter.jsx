import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import DOMPurify from "dompurify";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { renderMarkdown } from "@/lib/markdown";
import { HELP_CATEGORIES, HELP_ARTICLES } from "@/data/helpArticles";
import {
    Search, BookOpen, HelpCircle, LifeBuoy, Activity, ChevronRight,
    ArrowLeft, FileText, ShieldAlert, Rocket,
} from "lucide-react";

const HUB_LINKS = [
    { to: "/guide", label: "Full Guide", desc: "Deep dive into every STOIC system", icon: BookOpen, testid: "help-hub-guide" },
    { to: "/faq", label: "FAQ", desc: "Quick answers, searchable", icon: HelpCircle, testid: "help-hub-faq" },
    { to: "/support", label: "Support", desc: "Open a ticket — billing, technical, account", icon: LifeBuoy, testid: "help-hub-support" },
    { to: "/status", label: "System Status", desc: "Live component health", icon: Activity, testid: "help-hub-status" },
];

export default function HelpCenter() {
    const [query, setQuery] = useState("");
    const [cat, setCat] = useState("all");
    const [active, setActive] = useState(null); // article slug

    const results = useMemo(() => {
        const q = query.trim().toLowerCase();
        return HELP_ARTICLES.filter(a =>
            (cat === "all" || a.cat === cat) &&
            (!q || a.title.toLowerCase().includes(q)
                || a.summary.toLowerCase().includes(q)
                || a.body.toLowerCase().includes(q)));
    }, [query, cat]);

    const article = active ? HELP_ARTICLES.find(a => a.slug === active) : null;

    if (article) {
        return (
            <AppLayout>
                <button onClick={() => setActive(null)} data-testid="help-back"
                    className="flex items-center gap-2 text-xs font-mono tracking-widest text-[#00FF41] mb-6 hover:underline">
                    <ArrowLeft className="w-3.5 h-3.5" /> BACK TO HELP CENTER
                </button>
                <article className="bg-[#0A0A0A] border border-[#1F1F1F] p-6 sm:p-10 max-w-3xl"
                    data-testid="help-article">
                    <div className="text-[10px] font-mono tracking-widest text-[#52525B] mb-2 uppercase">
                        {HELP_CATEGORIES.find(c => c.id === article.cat)?.label}
                    </div>
                    <h1 className="text-2xl sm:text-3xl font-display font-bold text-white mb-6">{article.title}</h1>
                    <div dangerouslySetInnerHTML={{
                        __html: DOMPurify.sanitize(renderMarkdown(article.body),
                            { USE_PROFILES: { html: true } }),
                    }} />
                </article>
            </AppLayout>
        );
    }

    return (
        <AppLayout>
            <PageHeader title="Help Center" subtitle="Guides, answers and support — everything in one place." />

            {/* Hub links */}
            <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-8">
                {HUB_LINKS.map(h => (
                    <Link key={h.to} to={h.to} data-testid={h.testid}
                        className="bg-[#0A0A0A] border border-[#1F1F1F] hover:border-[#00FF41]/50 p-4 transition group">
                        <h.icon className="w-5 h-5 text-[#00FF41] mb-2" />
                        <div className="font-display text-sm text-white group-hover:text-[#00FF41] transition">{h.label}</div>
                        <div className="text-xs text-[#71717A] mt-0.5">{h.desc}</div>
                    </Link>
                ))}
            </div>

            {/* Legal quick links */}
            <div className="flex flex-wrap gap-2 mb-8 text-[10px] font-mono tracking-widest">
                <Link to="/terms" data-testid="help-legal-terms"
                    className="px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B] flex items-center gap-1.5">
                    <FileText className="w-3 h-3" /> TERMS OF USE
                </Link>
                <Link to="/privacy" data-testid="help-legal-privacy"
                    className="px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B] flex items-center gap-1.5">
                    <FileText className="w-3 h-3" /> PRIVACY POLICY
                </Link>
                <Link to="/risk-disclosure" data-testid="help-legal-risk"
                    className="px-3 py-1.5 border border-[#FFB000]/30 text-[#FFB000] hover:border-[#FFB000] flex items-center gap-1.5">
                    <ShieldAlert className="w-3 h-3" /> RISK DISCLOSURE
                </Link>
                <button onClick={async () => {
                    const api = (await import("@/lib/api")).default;
                    await api.put("/onboarding", { status: "pending", step: 0 });
                    window.location.href = "/";
                }} data-testid="help-restart-wizard"
                    className="px-3 py-1.5 border border-[#00FF41]/30 text-[#00FF41] hover:border-[#00FF41] flex items-center gap-1.5">
                    <Rocket className="w-3 h-3" /> RESTART ONBOARDING WIZARD
                </button>
            </div>

            {/* Search + categories */}
            <div className="relative mb-4 max-w-xl">
                <Search className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                <input value={query} onChange={e => setQuery(e.target.value)}
                    data-testid="help-search-input"
                    placeholder="Search guides… (e.g. EA, refund, 2FA, VPS)"
                    className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none pl-10 pr-3 py-3 text-sm transition-colors" />
            </div>
            <div className="flex flex-wrap gap-2 mb-6">
                {[{ id: "all", label: "All" }, ...HELP_CATEGORIES].map(c => (
                    <button key={c.id} onClick={() => setCat(c.id)}
                        data-testid={`help-cat-${c.id}`}
                        className={`px-3 py-1.5 text-[10px] font-mono tracking-widest uppercase border transition ${
                            cat === c.id ? "border-[#00FF41]/50 bg-[#00FF41]/10 text-[#00FF41]"
                                         : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"}`}>
                        {c.label}
                    </button>
                ))}
            </div>

            {/* Article list */}
            <div className="grid md:grid-cols-2 gap-3" data-testid="help-article-list">
                {results.map(a => (
                    <button key={a.slug} onClick={() => setActive(a.slug)}
                        data-testid={`help-article-${a.slug}`}
                        className="text-left bg-[#0A0A0A] border border-[#1F1F1F] hover:border-[#00FF41]/50 p-4 transition group">
                        <div className="text-[10px] font-mono tracking-widest text-[#52525B] uppercase mb-1">
                            {HELP_CATEGORIES.find(c => c.id === a.cat)?.label}
                        </div>
                        <div className="font-display text-sm text-white group-hover:text-[#00FF41] transition flex items-center justify-between">
                            {a.title}
                            <ChevronRight className="w-4 h-4 text-[#52525B] flex-shrink-0" />
                        </div>
                        <div className="text-xs text-[#71717A] mt-1">{a.summary}</div>
                    </button>
                ))}
                {results.length === 0 && (
                    <div className="text-sm text-[#52525B] font-mono" data-testid="help-no-results">
                        No articles match — try the <Link to="/faq" className="text-[#00FF41] hover:underline">FAQ</Link> or open a <Link to="/support" className="text-[#00FF41] hover:underline">support ticket</Link>.
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
