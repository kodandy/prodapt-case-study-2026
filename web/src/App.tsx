import { useMemo, useState, type ReactNode } from "react";
import { ServicesProvider, type AppServices } from "./app/services";
import { DashboardArea } from "./features/dashboard/DashboardArea";
import { MerchantArea } from "./features/merchant/MerchantArea";
import { OpsConsole } from "./features/ops/OpsConsole";
import { UserArea } from "./features/user/UserArea";
import { createApiClient } from "./lib/api";
import { createAuthStore } from "./lib/authStore";

type Area = "user" | "merchant" | "dashboard" | "ops";

const AREAS: readonly { id: Area; label: string }[] = [
  { id: "user", label: "User" },
  { id: "merchant", label: "Merchant" },
  { id: "dashboard", label: "Dashboard" },
  { id: "ops", label: "Ops console" },
];

function AreaView(props: { area: Area }): ReactNode {
  switch (props.area) {
    case "user":
      return <UserArea />;
    case "merchant":
      return <MerchantArea />;
    case "dashboard":
      return <DashboardArea />;
    case "ops":
      return <OpsConsole />;
  }
}

export function App(): ReactNode {
  const services = useMemo<AppServices>(() => {
    const store = createAuthStore();
    return { store, client: createApiClient({ fetch: (input, init) => fetch(input, init), store }) };
  }, []);
  const [area, setArea] = useState<Area>("user");

  return (
    <ServicesProvider services={services}>
      <header className="app-header">
        <h1>PayPlat</h1>
        <nav aria-label="Area">
          {AREAS.map((a) => (
            <button key={a.id} type="button" className="tab" aria-pressed={area === a.id} onClick={() => setArea(a.id)}>
              {a.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        <AreaView area={area} />
      </main>
    </ServicesProvider>
  );
}
