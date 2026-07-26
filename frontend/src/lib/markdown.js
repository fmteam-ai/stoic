// Tiny markdown→html renderer (shared) — same dialect as Terms.jsx:
// # h1, ## h2, ### h3, **bold**, `code`, - lists, 1. lists, --- hr, paragraphs.
export function renderMarkdown(md) {
    if (!md) return "";
    const lines = md.split("\n");
    const out = [];
    let buf = [];
    let inList = false;
    let inOrdered = false;

    const inline = (s) =>
        s.replace(/\*\*(.+?)\*\*/g, '<strong class="text-white">$1</strong>')
         .replace(/`([^`]+)`/g, '<code class="bg-[#1F1F1F] text-[#FFD700] px-1.5 py-0.5 rounded text-[12px] font-mono">$1</code>');

    const flushParagraph = () => {
        if (buf.length) {
            out.push(`<p class="text-[#A1A1AA] leading-7 mb-4">${inline(buf.join(" "))}</p>`);
            buf = [];
        }
    };
    const closeLists = () => {
        if (inList) { out.push("</ul>"); inList = false; }
        if (inOrdered) { out.push("</ol>"); inOrdered = false; }
    };

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
        buf.push(line);
    }
    flushParagraph(); closeLists();
    return out.join("\n");
}
