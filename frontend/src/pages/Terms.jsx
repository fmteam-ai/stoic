import { useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Loader2, FileText, ShieldAlert } from "lucide-react";
import axios from "axios";

// Tiny markdown→html renderer good enough for the curated TOS source.
// Handles: # h1, ## h2, ### h3, **bold**, lists (- / 1.), --- hr, paragraphs.
function renderMarkdown(md) {
    if (!md) return "";
    const lines = md.split("\n");
    const out = [];
    let buf = [];
    let inList = false;
    let inOrdered = false;

    const flushParagraph = () => {
        if (buf.length) {
            // Join first so bold/code spans that cross line breaks render correctly.
            out.push(`<p class="text-[#A1A1AA] leading-7 mb-4">${inline(buf.join(" "))}</p>`);
            buf = [];
        }
    };
    const closeLists = () => {
        if (inList) { out.push("</ul>"); inList = false; }
        if (inOrdered) { out.push("</ol>"); inOrdered = false; }
    };
    const inline = (s) =>
        s.replace(/\*\*(.+?)\*\*/g, '<strong class="text-white">$1</strong>')
         .replace(/`([^`]+)`/g, '<code class="bg-[#1F1F1F] text-[#FFD700] px-1.5 py-0.5 rounded text-[12px] font-mono">$1</code>');

    for (let raw of lines) {
        const line = raw.replace(/\r$/, "");
        if (line.trim() === "") { flushParagraph(); closeLists(); continue; }
        if (line.startsWith("---")) { flushParagraph(); closeLists(); out.push('<hr class="border-[#1F1F1F] my-8" />'); continue; }
        if (line.startsWith("### ")) {
            flushParagraph(); closeLists();
            out.push(`<h3 class="text-base font-display font-bold text-white mt-6 mb-3 tracking-wide">${inline(line.slice(4))}</h3>`);
            continue;
        }
        if (line.startsWith("## ")) {
            flushParagraph(); closeLists();
            out.push(`<h2 class="text-xl font-display font-bold text-[#FFD700] mt-8 mb-3 tracking-wide border-b border-[#1F1F1F] pb-2">${inline(line.slice(3))}</h2>`);
            continue;
        }
        if (line.startsWith("# ")) {
            flushParagraph(); closeLists();
            out.push(`<h1 class="text-3xl sm:text-4xl font-display font-bold text-white mt-2 mb-4 tracking-wide">${inline(line.slice(2))}</h1>`);
            continue;
        }
        const ulMatch = line.match(/^[-*]\s+(.+)/);
        if (ulMatch) {
            flushParagraph();
            if (!inList) { closeLists(); out.push('<ul class="list-disc list-outside ml-6 mb-4 space-y-1.5 text-[#A1A1AA]">'); inList = true; }
            out.push(`<li>${inline(ulMatch[1])}</li>`);
            continue;
        }
        const olMatch = line.match(/^(\d+)\.\s+(.+)/);
        if (olMatch) {
            flushParagraph();
            if (!inOrdered) { closeLists(); out.push('<ol class="list-decimal list-outside ml-6 mb-4 space-y-1.5 text-[#A1A1AA]">'); inOrdered = true; }
            out.push(`<li>${inline(olMatch[2])}</li>`);
            continue;
        }
        if (line.startsWith("*") && line.endsWith("*") && line.length > 2) {
            flushParagraph(); closeLists();
            out.push(`<p class="text-[#52525B] italic text-sm mb-4">${inline(line.slice(1, -1))}</p>`);
            continue;
        }
        // Plain paragraph line — defer inline replacement until paragraph flush.
        if (inList || inOrdered) closeLists();
        buf.push(line);
    }
    flushParagraph();
    closeLists();
    return out.join("\n");
}

// Public-friendly fetch — uses raw axios so the page works pre-login too.
const RAW_API = `${process.env.REACT_APP_BACKEND_URL}/api`;

export default function Terms() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");

    useEffect(() => {
        let mounted = true;
        axios.get(`${RAW_API}/terms`, { timeout: 15000 })
            .then(r => { if (mounted) setData(r.data); })
            .catch(e => { if (mounted) setErr(e?.message || "Failed to load terms"); })
            .finally(() => { if (mounted) setLoading(false); });
        return () => { mounted = false; };
    }, []);

    return (
        <AppLayout>
            <div className="max-w-4xl mx-auto">
                <PageHeader
                    title="Terms of Use"
                    subtitle={data ? `Version ${data.version} · Last updated ${data.last_updated}` : "Loading…"}
                    testid="terms-page-header"
                />

                {loading ? (
                    <div className="flex items-center justify-center py-20 text-[#A1A1AA]" data-testid="terms-loading">
                        <Loader2 className="w-5 h-5 mr-2 animate-spin" />
                        Loading Terms of Use…
                    </div>
                ) : err ? (
                    <div className="border border-red-900/40 bg-red-950/20 text-red-300 p-4 mb-6 flex items-start gap-2" data-testid="terms-error">
                        <ShieldAlert className="w-4 h-4 mt-0.5" />
                        <div>
                            <div className="font-mono text-xs tracking-wide">UNABLE TO LOAD TERMS</div>
                            <div className="text-sm mt-1">{err}</div>
                        </div>
                    </div>
                ) : (
                    <article className="bg-[#0A0A0A] border border-[#1F1F1F] p-6 sm:p-10" data-testid="terms-content">
                        <div className="flex items-center gap-2 text-[#52525B] font-mono text-[10px] tracking-widest mb-6">
                            <FileText className="w-3 h-3" />
                            CANONICAL · NON-NEGOTIABLE
                        </div>
                        <div
                            className="prose-stoic"
                            dangerouslySetInnerHTML={{ __html: renderMarkdown(data?.markdown || "") }}
                        />
                    </article>
                )}
            </div>
        </AppLayout>
    );
}
