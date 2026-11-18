import {defineConfig, loadEnv} from 'vite';

// Hosts and ports come from the repo-root .env (or, in a container, from the
// process environment — loadEnv with an empty prefix includes both).
export default defineConfig(({mode}) => {
    const env = loadEnv(mode, '..', '');
    const required = (name: string): string => {
        const value = env[name];
        if (!value) throw new Error(`${name} is not set (expected in ../.env or the environment)`);
        return value;
    };

    return {
        server: {
            host: required('frontend_host'),
            port: Number(required('frontend_port')),
            strictPort: true,
            proxy: {
                "/socket.io": {
                    target: `http://${required('frontend_backend_host')}:${required('frontend_backend_port')}`,
                    ws: true,
                }
            }
        },
        define: {
            // Only the endpoint is exposed to the browser bundle, not other OTEL_* vars
            // (e.g. OTEL_EXPORTER_OTLP_HEADERS may hold credentials).
            __OTLP_TRACES_ENDPOINT__: JSON.stringify(
                `${required('OTEL_EXPORTER_OTLP_ENDPOINT').replace(/\/$/, '')}/v1/traces`,
            ),
        },
    };
});
