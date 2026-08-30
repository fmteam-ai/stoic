import { Component } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

/* Route-level error boundary (iter-158 review P0-6): a crashed lazy chunk
   or render error must NEVER leave a blank document on a trading surface. */
export class RouteErrorBoundary extends Component {
    constructor(props) {
        super(props);
        this.state = { error: null, id: null };
    }

    static getDerivedStateFromError(error) {
        return { error, id: `ERR-${Date.now().toString(36).toUpperCase()}` };
    }

    componentDidCatch(error, info) {
        // eslint-disable-next-line no-console
        console.error("Route render error", this.state.id, error, info);
    }

    retry = () => {
        const msg = String(this.state.error?.message || "");
        // chunk-load failures need a real reload to fetch the new build
        if (/Loading chunk|Failed to fetch dynamically imported/i.test(msg)) {
            window.location.reload();
            return;
        }
        this.setState({ error: null, id: null });
    };

    render() {
        if (!this.state.error) return this.props.children;
        return (
            <div className="min-h-[60vh] flex items-center justify-center p-6"
                data-testid="route-error-boundary">
                <div className="max-w-md w-full border border-[#FF3B30]/40 bg-[#FF3B30]/5 p-6 text-center">
                    <AlertTriangle className="w-8 h-8 text-[#FF3B30] mx-auto mb-3" />
                    <div className="font-display text-lg text-[#FAFAFA] mb-1">
                        This page failed to load
                    </div>
                    <div className="text-xs text-[#71717A] mb-1 break-all">
                        {String(this.state.error?.message || this.state.error)}
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] mb-4"
                        data-testid="route-error-correlation-id">
                        Correlation ID: {this.state.id}
                    </div>
                    <button onClick={this.retry} data-testid="route-error-retry"
                        className="px-4 py-2 text-xs font-mono tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 inline-flex items-center gap-1.5">
                        <RefreshCw className="w-3.5 h-3.5" /> RETRY
                    </button>
                </div>
            </div>
        );
    }
}
