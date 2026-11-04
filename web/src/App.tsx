import { useMemo, useState, type ReactNode } from "react";
import { ServicesProvider, type AppServices } from "./app/services";
import { MerchantArea } from "./features/merchant/MerchantArea";
import { UserArea } from "./features/user/UserArea";
import { createApiClient } from "./lib/api";
import { createAuthStore } from "./lib/authStore";

type Area = "user" | "merchant";

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
          {(["user", "merchant"] as const).map((a) => (
            <button key={a} type="button" className="tab" aria-pressed={area === a} onClick={() => setArea(a)}>
              {a === "user" ? "User" : "Merchant"}
            </button>
          ))}
        </nav>
      </header>
      <main>{area === "user" ? <UserArea /> : <MerchantArea />}</main>
    </ServicesProvider>
  );
}
