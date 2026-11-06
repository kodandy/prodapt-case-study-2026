import { createContext, useContext, useSyncExternalStore, type ReactNode } from "react";
import type { OpsApiClient, OpsSessionState, OpsSessionStore } from "../../lib/opsSession";

export interface OpsServices {
  store: OpsSessionStore;
  client: OpsApiClient;
}

const OpsServicesContext = createContext<OpsServices | null>(null);

/** The ops principal's credentials live in their own memory-only store, apart from the user/merchant store. */
export function OpsServicesProvider(props: { services: OpsServices; children: ReactNode }): ReactNode {
  return <OpsServicesContext.Provider value={props.services}>{props.children}</OpsServicesContext.Provider>;
}

/** null when no provider is mounted: the console then offers only the merchant-scoped demo sign-in. */
export function useOptionalOpsServices(): OpsServices | null {
  return useContext(OpsServicesContext);
}

export function useOpsServices(): OpsServices {
  const services = useContext(OpsServicesContext);
  if (services === null) throw new Error("OpsServicesProvider missing");
  return services;
}

const NO_STORE_STATE: OpsSessionState = { sessionToken: null, role: null, opsAiToken: null };

export function useOpsSessionState(services: OpsServices | null): OpsSessionState {
  return useSyncExternalStore(
    (onChange) => (services === null ? () => undefined : services.store.subscribe(onChange)),
    () => (services === null ? NO_STORE_STATE : services.store.get()),
  );
}
