export interface BrowseEntry {
  name: string;
  path: string;
}

export interface BrowseResult {
  path: string;
  parent: string | null;
  entries: BrowseEntry[];
}

export interface InitResult {
  target: string;
  written: string[];
  namespace: string;
}

export interface InitError {
  error: string;
  exists?: boolean;
}

export interface ScaffoldNode {
  id: string;
  file: string;
}

export interface ScaffoldResult {
  project: string;
  path: string;
  synapse: { id: string };
  neurons: ScaffoldNode[];
  effectors: ScaffoldNode[];
  engrams: ScaffoldNode[];
}
