import React from "react";

export class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { error: null };
    }

    static getDerivedStateFromError(error) {
        return { error };
    }

    componentDidCatch(error, info) {
        // eslint-disable-next-line no-console
        console.error("UI error boundary caught:", error, info?.componentStack);
    }

    render() {
        if (!this.state.error) return this.props.children;
        return (
            <div data-testid="error-boundary-fallback" style={{
                minHeight: "100vh", display: "flex", flexDirection: "column",
                alignItems: "center", justifyContent: "center",
                background: "#0B0D10", color: "#E4E4E7",
                fontFamily: "monospace", padding: "2rem", textAlign: "center",
            }}>
                <div style={{ fontSize: "2rem", marginBottom: "1rem" }}>⚠</div>
                <h1 style={{ fontSize: "1.1rem", marginBottom: "0.5rem" }}>
                    Something went wrong rendering this page
                </h1>
                <p style={{ color: "#71717A", fontSize: "0.85rem", maxWidth: 480 }}>
                    Your trading engine and account are unaffected — this is a
                    display error only.
                </p>
                <button
                    data-testid="error-boundary-reload-btn"
                    onClick={() => window.location.reload()}
                    style={{
                        marginTop: "1.5rem", padding: "0.6rem 1.6rem",
                        background: "#10F2C5", color: "#0B0D10", border: "none",
                        borderRadius: 9999, cursor: "pointer", fontWeight: 700,
                    }}
                >
                    RELOAD
                </button>
            </div>
        );
    }
}

export default ErrorBoundary;
