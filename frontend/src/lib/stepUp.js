// Singleton bridge between the axios interceptor and the StepUpDialog.
let handler = null;

export function registerStepUpHandler(fn) {
    handler = fn;
}

export function requestStepUp(detail) {
    if (!handler) return Promise.reject(new Error("step_up_unavailable"));
    return handler(detail);
}
