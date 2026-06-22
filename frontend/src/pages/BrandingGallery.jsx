/**
 * Internal route /branding — quick visual A/B for the 3 candidate logo marks.
 * Not linked in the sidebar; intended for stakeholder review during the
 * branding pass. Safe to leave in place — it's gated behind ProtectedRoute.
 */
import { StoicPillar, StoicWedge, StoicSeal } from "@/components/StoicLogo";
import { AppLayout, PageHeader } from "@/components/AppLayout";

const OPTIONS = [
    {
        name: "THE PILLAR",
        Mark: StoicPillar,
        thesis: "Fluted Doric column doubling as a 4-bar chart. Greek architecture + price action in one glyph.",
        accent: "border-[#00FF41]/40 bg-[#00FF41]/5",
        testid: "logo-pillar",
    },
    {
        name: "THE WEDGE",
        Mark: StoicWedge,
        thesis: "Ascending mark with a horizontal discipline line. A chart arrow that knows when to stop.",
        accent: "border-[#FFB000]/40 bg-[#FFB000]/5",
        testid: "logo-wedge",
    },
    {
        name: "THE SEAL",
        Mark: StoicSeal,
        thesis: "Octagonal institutional seal with an etched Σ — coin / vault energy.",
        accent: "border-[#A1A1AA]/30 bg-[#121212]",
        testid: "logo-seal",
    },
];

export default function BrandingGallery() {
    return (
        <AppLayout>
            <PageHeader
                title="Logo Candidates"
                subtitle="Internal — pick the mark that fits the brand best."
                testid="branding-header"
            />
            <div className="p-4 md:p-8 space-y-8 max-w-5xl">
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    {OPTIONS.map(({ name, Mark, thesis, accent, testid }) => (
                        <div key={name} data-testid={testid}
                            className={`bg-[#0A0A0A] border ${accent} p-6 flex flex-col items-center text-center`}>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-6">{name}</div>
                            <Mark size={120} className="mb-6" />
                            <div className="flex items-center gap-3 mb-4">
                                <Mark size={28} />
                                <div className="font-display font-bold text-sm tracking-[0.18em]">STOIC</div>
                            </div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">SMALL · 16px · 14px</div>
                            <div className="flex items-center gap-2 mb-4">
                                <Mark size={16} />
                                <Mark size={20} />
                                <Mark size={28} />
                                <Mark size={48} />
                            </div>
                            <p className="text-xs text-[#A1A1AA] leading-relaxed">{thesis}</p>
                        </div>
                    ))}
                </div>

                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 text-xs text-[#A1A1AA] font-mono">
                    Currently active: <span className="text-[#00FF41]">THE PILLAR</span>
                    {" "}— change <code className="text-white">StoicMark = StoicPillar</code> in <code className="text-white">StoicLogo.jsx</code> to swap.
                </div>
            </div>
        </AppLayout>
    );
}
