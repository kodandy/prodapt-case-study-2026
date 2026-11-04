import { createContext, useContext, useMemo, useSyncExternalStore, type ReactNode } from "react";
import type { ApiClient, ApiResult } from "../lib/api";
import type { AuthState, AuthStore } from "../lib/authStore";
import { messageFor } from "../lib/errors";
import { createPendingWrites, type PendingWrites } from "../lib/pendingWrites";

export interface AppServices {
  store: AuthStore;
  client: ApiClient;
}

export interface ProvidedServices extends AppServices {
  pendingWrites: PendingWrites;
}

const ServicesContext = createContext<ProvidedServices | null>(null);

export function ServicesProvider(props: { services: AppServices; children: ReactNode }): ReactNode {
  const { store, client } = props.services;
  const value = useMemo(() => ({ store, client, pendingWrites: createPendingWrites(store) }), [store, client]);
  return <ServicesContext.Provider value={value}>{props.children}</ServicesContext.Provider>;
}

export function useServices(): ProvidedServices {
  const services = useContext(ServicesContext);
  if (services === null) throw new Error("ServicesProvider missing");
  return services;
}

export function useAuthState(): AuthState {
  const { store } = useServices();
  return useSyncExternalStore(
    (onChange) => store.subscribe(onChange),
    () => store.get(),
  );
}

export function errorMessageOf<T>(result: ApiResult<T>): string | null {
  return result.ok ? null : messageFor(result.code);
}
