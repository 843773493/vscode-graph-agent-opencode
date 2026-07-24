import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import WarmActionDialog from "./WarmActionDialog";

interface WarmConfirmOptions {
  title?: string;
  message: string;
  confirmText?: string;
  danger?: boolean;
}

interface PendingConfirmation extends WarmConfirmOptions {
  id: number;
  resolve: (confirmed: boolean) => void;
}

type WarmConfirm = (options: WarmConfirmOptions) => Promise<boolean>;

const WarmConfirmContext = createContext<WarmConfirm | null>(null);

export function useWarmConfirm(): WarmConfirm {
  const confirm = useContext(WarmConfirmContext);
  if (!confirm) {
    throw new Error("useWarmConfirm 必须在 WarmConfirmProvider 内使用");
  }
  return confirm;
}

export default function WarmConfirmProvider({ children }: { children: ReactNode }) {
  const [queue, setQueue] = useState<PendingConfirmation[]>([]);
  const nextIdRef = useRef(1);
  const confirmedIdRef = useRef<number | null>(null);
  const active = queue[0] ?? null;

  const confirm = useCallback<WarmConfirm>((options) => {
    return new Promise<boolean>((resolve) => {
      const id = nextIdRef.current;
      nextIdRef.current += 1;
      setQueue((current) => [...current, { ...options, id, resolve }]);
    });
  }, []);

  const closeActive = () => {
    if (!active) {
      return;
    }
    if (confirmedIdRef.current === active.id) {
      confirmedIdRef.current = null;
    } else {
      active.resolve(false);
    }
    setQueue((current) => current.filter((item) => item.id !== active.id));
  };

  return (
    <WarmConfirmContext.Provider value={confirm}>
      {children}
      <WarmActionDialog
        open={active !== null}
        title={active?.title ?? "请确认"}
        description={active?.message}
        confirmText={active?.confirmText ?? "确认"}
        danger={active?.danger ?? false}
        onClose={closeActive}
        onConfirm={async () => {
          if (!active) {
            throw new Error("确认目标已失效");
          }
          confirmedIdRef.current = active.id;
          active.resolve(true);
        }}
      />
    </WarmConfirmContext.Provider>
  );
}
