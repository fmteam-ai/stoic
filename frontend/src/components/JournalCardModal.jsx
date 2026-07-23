import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { X, Loader2, Copy, RotateCcw, Ban, BookOpen } from "lucide-react";

const GRADE_COLOR = {
    A: "text-[#00FF41] border-[#00FF41]/40", B: "text-[#00FF41] border-[#00FF41]/40",
    C: "text-[#FFB000] border-[#FFB000]/40", D: "text-[#FF3B30] border-[#FF3B30]/40",
    F: "text-[#FF3B30] border-[#FF3B30]/40",
};

export const JournalCardBody = ({ card, trade }) => {
    const win = card.verdict === "WIN";
    return (
        <div data-testid="journal-card-body">
            <div className="flex items-start justify-between gap-3">
                <div>
                    <div className={`font-mono text-[10px] tracking-widest ${win ? "text-[#00FF41]" : card.verdict === "LOSS" ? "text-[#FF3B30]" : "text-[#A1A1AA]"}`}>
                        {card.verdict} · {trade.symbol} {String(trade.action || "").toUpperCase()}
                    </div>
                    <div className="font-display font-bold text-xl text-white mt-1" data-testid="journal-card-title">{card.title}</div>
                </div>
                <div className={`border ${GRADE_COLOR[card.grade] || "text-[#A1A1AA] border-[#1F1F1F]"} w-12 h-12 flex items-center justify-center font-display font-bold text-2xl shrink-0`}
                    data-testid="journal-card-grade">
                    {card.grade || "—"}
                </div>
            </div>
            <div className="grid grid-cols-3 gap-2 mt-4 font-mono text-xs">
                <div className="border border-[#1F1F1F] p-2">
                    <div className="text-[9px] tracking-widest text-[#52525B]">ENTRY</div>
                    <div className="text-white mt-0.5">{trade.entry_price ?? "—"}</div>
                </div>
                <div className="border border-[#1F1F1F] p-2">
                    <div className="text-[9px] tracking-widest text-[#52525B]">EXIT</div>
                    <div className="text-white mt-0.5">{trade.exit_price ?? "—"}</div>
                </div>
                <div className="border border-[#1F1F1F] p-2">
                    <div className="text-[9px] tracking-widest text-[#52525B]">P&L</div>
                    <div className={`mt-0.5 ${parseFloat(trade.pnl) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`} data-testid="journal-card-pnl">
                        ${parseFloat(trade.pnl || 0).toFixed(2)}
                    </div>
                </div>
            </div>
            <p className="text-sm text-[#D4D4D8] leading-relaxed mt-4">{card.summary}</p>
            {card.what_went_right && (
                <div className="mt-3">
                    <div className="font-mono text-[9px] tracking-widest text-[#00FF41]">WHAT WENT RIGHT</div>
                    <p className="text-xs text-[#A1A1AA] mt-1">{card.what_went_right}</p>
                </div>
            )}
            {card.what_went_wrong && (
                <div className="mt-3">
                    <div className="font-mono text-[9px] tracking-widest text-[#FF3B30]">WHAT WENT WRONG</div>
                    <p className="text-xs text-[#A1A1AA] mt-1">{card.what_went_wrong}</p>
                </div>
            )}
            {card.lesson && (
                <div className="border-l-2 border-[#FFD700] pl-3 mt-4">
                    <div className="font-mono text-[9px] tracking-widest text-[#FFD700]">LESSON</div>
                    <p className="text-sm text-white italic mt-1" data-testid="journal-card-lesson">{card.lesson}</p>
                </div>
            )}
            {Array.isArray(card.hashtags) && card.hashtags.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-4">
                    {card.hashtags.map((h, i) => (
                        <span key={i} className="font-mono text-[10px] text-[#0099FF]">#{h}</span>
                    ))}
                </div>
            )}
        </div>
    );
};

export const JournalCardModal = ({ trade, onClose }) => {
    const [d, setD] = useState(null);
    const [busy, setBusy] = useState(true);

    const generate = useCallback(async (force = false) => {
        setBusy(true);
        try {
            const { data } = await api.post(`/journal/${trade.id}/card${force ? "?force=true" : ""}`);
            setD(data);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setBusy(false);
        }
    }, [trade.id]);

    useEffect(() => { generate(false); }, [generate]);

    const shareUrl = d?.share_id && !d.revoked
        ? `${window.location.origin}/j/${d.share_id}` : null;

    const copyLink = async () => {
        await navigator.clipboard.writeText(shareUrl);
        toast.success("Public link copied — post it anywhere");
    };

    const revoke = async () => {
        try {
            await api.delete(`/journal/${trade.id}/card`);
            setD({ ...d, revoked: true });
            toast.success("Share link revoked");
        } catch (e) {
            toast.error(formatApiError(e));
        }
    };

    return (
        <div className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#1F1F1F] max-w-lg w-full max-h-[90vh] overflow-y-auto p-6"
                onClick={e => e.stopPropagation()} data-testid="journal-card-modal">
                <div className="flex items-center justify-between mb-4">
                    <div className="flex items-center gap-2">
                        <BookOpen className="w-4 h-4 text-[#FFD700]" />
                        <span className="font-display font-bold text-white">Trade Journal Card</span>
                    </div>
                    <button onClick={onClose} data-testid="journal-card-close" className="text-[#A1A1AA] hover:text-white">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                {busy && (
                    <div className="flex items-center gap-2 text-[#A1A1AA] font-mono text-xs py-8 justify-center">
                        <Loader2 className="w-4 h-4 animate-spin" /> WRITING YOUR POST-MORTEM…
                    </div>
                )}
                {!busy && d && (
                    <>
                        <JournalCardBody card={d.card} trade={d.trade} />
                        <div className="flex items-center gap-2 mt-6 pt-4 border-t border-[#1F1F1F] flex-wrap">
                            {shareUrl ? (
                                <>
                                    <button onClick={copyLink} data-testid="journal-copy-link"
                                        className="flex items-center gap-1.5 px-3 py-2 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 text-xs font-mono tracking-widest">
                                        <Copy className="w-3 h-3" /> COPY PUBLIC LINK
                                    </button>
                                    <button onClick={revoke} data-testid="journal-revoke-link"
                                        className="flex items-center gap-1.5 px-3 py-2 border border-[#1F1F1F] text-[#A1A1AA] hover:text-[#FF3B30] hover:border-[#FF3B30]/40 text-xs font-mono tracking-widest">
                                        <Ban className="w-3 h-3" /> REVOKE
                                    </button>
                                </>
                            ) : (
                                <span className="font-mono text-[10px] tracking-widest text-[#52525B]">SHARE LINK REVOKED</span>
                            )}
                            <button onClick={() => generate(true)} data-testid="journal-regenerate"
                                className="flex items-center gap-1.5 px-3 py-2 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white text-xs font-mono tracking-widest ml-auto">
                                <RotateCcw className="w-3 h-3" /> REGENERATE
                            </button>
                        </div>
                    </>
                )}
            </div>
        </div>
    );
};
