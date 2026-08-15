import { useState } from "react";
import { StartScreen } from "./components/StartScreen";
import { Workspace } from "./components/Workspace";
import { rememberProject } from "./recents";
import type { InitResult } from "./types";
import { useThemeMode } from "./theme";

export function App() {
  // Subscribing at the root is what makes a theme flip repaint the whole
  // tree, so every literal `C.x` read  -  including SVG stroke= attributes,
  // which cannot resolve var()  -  picks up the new palette.
  useThemeMode();
  const [projectPath, setProjectPath] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  function open(path: string, name: string) {
    rememberProject({ path, name });
    setNotice(null);
    setProjectPath(path);
  }

  function handleScaffolded(result: InitResult) {
    // The scaffolder returns the project directory it created, whose last
    // segment is the brain's name.
    open(result.target, result.target.split(/[\\/]/).filter(Boolean).pop() ?? result.target);
    // A repository that couldn't be started never fails the scaffold, so it
    // has to be said out loud here - otherwise the History tab is the first
    // the user hears of it.
    if (result.git && !result.git.repo) {
      setNotice(`Project created, but no git repository: ${result.git.note ?? "git refused."}`);
    }
  }

  if (projectPath) {
    return (
      <Workspace
        projectPath={projectPath}
        notice={notice}
        onBack={() => setProjectPath(null)}
      />
    );
  }
  return <StartScreen onScaffolded={handleScaffolded} onOpen={open} />;
}
