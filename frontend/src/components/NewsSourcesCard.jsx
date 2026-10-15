import { useCallback, useEffect, useState } from "react";
import { Newspaper, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const ago = (sec) => {
    if (sec == null) return "never";
    if (sec < 90) return `${sec}s ago`;
    if (sec < 5400) return `${Math.round(sec / 60)}m ago`;
    return `${Math.round(sec / 3600)}h ago`;
};

const TONE = {
    OK: "border-[#00FF41]/40 text-[#00FF41]",
    FAILSAFE: "border-[#FF3B30]/40 text-[#FF3B30]",
    DEGRADED: "border-[#FFB000]/40 text-[#FFB000]",
    STALE: "border-[#FFB000]/40 text-[#FFB000]",
    DOWN: "border-[#FF3B30]/40 text-[#FF3B30]",
    IDLE: "border-[#1F1F1F] text-[#52525B]",
    OFF: "border-[#1F1F1F] text-[#52525B]",
};

function Row({ name, status, detail, testid }) {
    const mark = status === "OK" ? "✓" : status === "OFF" || status === "IDLE" ? "–" : "!";
    return (
        <div className="flex items-start justify-between gap-3 py-1.5 border-b border-[#141414] last:border-0 font-mono text-[11px]" data-testid={testid}>
            <div className="min-w-0">
                <div className="text-[#A1A1AA]">{name}</div>
                {detail && <div className="text-[#52525B] text-[10px] truncate">{detail}</div>}
            </div>
            <span className={`shrink-0 px-1.5 py-0.5 border text-[10px] tracking-widest ${TONE[status] || TONE.IDLE}`}>{mark} {status}</span>
        </div>
    );
}

// v1.60.39 — where signal["sentiment"] / news_ai actually come from in production (NewsAPI is off there).
export function NewsSourcesCard() {
    const [d, setD] = useState(null);
    const load = useCallback(() => api.get("/ops/news-sources").then((r) => setD(r.data)).catch(() => setD({ error: true })), []);
    useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
    const cal = d?.calendar || {};
    const rss = d?.rss_ai || {};
    const napi = d?.newsapi || {};
    const bases = rss.bases || [];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="news-sources-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Newspaper className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">News Sources</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">SENTIMENT ← {(d?.sentiment_source || "rss_ai").toUpperCase()}</span>
                <button onClick={load} className="text-[#52525B] hover:text-white" data-testid="news-sources-refresh" aria-label="Refresh"><RefreshCw className="w-3 h-3" /></button>
            </div>
            <div className="px-4 py-2">
                {d?.error && <div className="font-mono text-[11px] text-[#FF3B30]">unavailable</div>}
                {d && !d.error && (
                    <>
                        <Row name="Economic calendar" status={cal.status || "IDLE"} testid="news-source-calendar"
                            detail={cal.failsafe_active
                                ? `FAIL-SAFE · both sources > ${cal.failsafe_after_min ?? 360} min stale — default US freeze slots (12:30 / 14:00 UTC, 18:00 on FOMC days) applied until a source recovers`
                                : cal.stale
                                    ? `STALE · active source older than ${cal.stale_after_min ?? 120} min — freeze windows may miss new prints`
                                    : `${cal.active_source || "no source"} active · ${cal.cached_events ?? 0} events merged`} />
                        {cal.sources && Object.entries(cal.sources).map(([name, s]) => (
                            <Row key={name} name={`  ↳ ${name}`} status={s.status || "IDLE"} testid={`news-source-calendar-${name}`}
                                detail={s.last_error || (s.last_fetch_at ? `${s.events} events · fetched ${ago(Math.round((s.age_min || 0) * 60))}` : "never fetched")} />
                        ))}
                        {cal.attribution?.url && (
                            <div className="font-mono text-[9px] text-[#3F3F46] pt-1" data-testid="calendar-attribution">
                                calendar data in part from <a href={cal.attribution.url} target="_blank" rel="noreferrer" className="underline hover:text-[#A1A1AA]">financecalendar.com</a>
                            </div>
                        )}
                        <Row name="RSS wire + AI scoring" status={rss.status || "IDLE"} testid="news-source-rss-ai"
                            detail={rss.last_error || rss.last_llm_error
                                || (rss.last_fetch_at ? `fetched ${ago(rss.age_sec)} · ${rss.rss_items ?? 0} RSS · ${rss.headlines ?? 0} scored` : "no fetch recorded yet")} />
                        <Row name="NewsAPI" status={napi.configured ? "OK" : "OFF"} testid="news-source-newsapi"
                            detail={napi.configured ? "supplementing headlines" : "not configured — not required (free tier delays articles up to 24 h)"} />
                        {bases.length > 0 && (
                            <div className="pt-2 flex flex-wrap gap-2" data-testid="news-source-bases">
                                {bases.map((b) => (
                                    <span key={b.base} className="font-mono text-[10px] px-1.5 py-0.5 border border-[#1F1F1F] text-[#A1A1AA]">
                                        {b.base} {b.net > 0 ? "+" : ""}{b.net} · {String(b.label || "").replace("_", " ")} · n={b.headlines}
                                    </span>
                                ))}
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}

export default NewsSourcesCard;
