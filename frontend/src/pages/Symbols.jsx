import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";

export default function Symbols() {
    const [supported, setSupported] = useState([]);
    const [quotes, setQuotes] = useState({});
    const [err, setErr] = useState("");
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/market/symbols");
            setSupported(data.symbols);
            const { data: q } = await api.get(`/market/quotes?symbols=${data.symbols.join(",")}`);
            const map = {};
            for (const x of q.quotes) { if (x.symbol) map[x.symbol] = x; }
            setQuotes(map);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    return (
        <AppLayout>
            <PageHeader
                title="Symbols"
                subtitle="All tradeable instruments supported via Alpha Vantage. Add them to your bot from the Bot Config page."
                testid="symbols-header"
            />
            <div className="p-4 md:p-8 space-y-4">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}
                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING SYMBOLS…</div>
                ) : (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] overflow-x-auto" data-testid="symbols-table-wrapper">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b border-[#1F1F1F]">
                                    <th className="px-4 py-3 text-left font-mono text-[10px] text-[#52525B] tracking-widest">SYMBOL</th>
                                    <th className="px-4 py-3 text-left font-mono text-[10px] text-[#52525B] tracking-widest">ASSET CLASS</th>
                                    <th className="px-4 py-3 text-right font-mono text-[10px] text-[#52525B] tracking-widest">PRICE</th>
                                    <th className="px-4 py-3 text-right font-mono text-[10px] text-[#52525B] tracking-widest">BID</th>
                                    <th className="px-4 py-3 text-right font-mono text-[10px] text-[#52525B] tracking-widest">ASK</th>
                                </tr>
                            </thead>
                            <tbody>
                                {supported.map(s => {
                                    const q = quotes[s];
                                    const isCrypto = ["BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD"].includes(s);
                                    const isGold = s === "XAUUSD";
                                    return (
                                        <tr key={s} className="border-b border-[#1F1F1F] hover:bg-[#121212] transition-colors" data-testid={`symbol-row-${s}`}>
                                            <td className="px-4 py-3 font-mono font-medium">{s}</td>
                                            <td className="px-4 py-3 font-mono text-xs text-[#A1A1AA] tracking-widest">
                                                {isCrypto ? "CRYPTO" : isGold ? "COMMODITY" : "FOREX"}
                                            </td>
                                            <td className="px-4 py-3 font-mono text-right">{q?.price ? q.price.toLocaleString(undefined, { maximumFractionDigits: 5 }) : "—"}</td>
                                            <td className="px-4 py-3 font-mono text-right text-[#A1A1AA]">{q?.bid ? q.bid.toLocaleString(undefined, { maximumFractionDigits: 5 }) : "—"}</td>
                                            <td className="px-4 py-3 font-mono text-right text-[#A1A1AA]">{q?.ask ? q.ask.toLocaleString(undefined, { maximumFractionDigits: 5 }) : "—"}</td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                )}
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    NOTE · Free-tier rate limit may cause some quotes to display as “—”. Live prices cached 60s.
                </div>
            </div>
        </AppLayout>
    );
}
