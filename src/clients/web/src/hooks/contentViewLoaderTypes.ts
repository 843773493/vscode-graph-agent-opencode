import type { Dispatch, SetStateAction } from "react";
import type { AppState } from "../types/frontend";

export type SetAppState = Dispatch<SetStateAction<AppState>>;
export type RefreshOptions = { silent?: boolean };
