import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Construction } from "lucide-react";

export default function ComingSoon({ title, subtitle, blurb, testid }) {
    return (
        <AppLayout>
            <div className="space-y-6" data-testid={testid || "coming-soon-page"}>
                <PageHeader title={title} subtitle={subtitle} testid="coming-soon-header" />
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-12 flex flex-col items-center text-center gap-4">
                    <Construction className="w-10 h-10 text-[#FFB000]" />
                    <div className="font-mono text-xs tracking-[0.3em] text-[#FFB000]">COMING SOON</div>
                    <p className="text-sm text-[#A1A1AA] max-w-md">{blurb}</p>
                </div>
            </div>
        </AppLayout>
    );
}
