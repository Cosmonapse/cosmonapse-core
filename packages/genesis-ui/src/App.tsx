import { useState } from "react";
import { StartForm } from "./components/StartForm";
import { GenesisCanvas } from "./components/GenesisCanvas";
import type { InitResult } from "./types";

export function App() {
  const [scaffoldPath, setScaffoldPath] = useState<string | null>(null);

  function handleScaffolded(result: InitResult) {
    setScaffoldPath(result.target);
  }

  if (scaffoldPath) {
    return <GenesisCanvas initialPath={scaffoldPath} onBack={() => setScaffoldPath(null)} />;
  }
  return <StartForm onScaffolded={handleScaffolded} />;
}
