import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiResult } from "../lib/api";
import { errorMessageOf } from "./services";

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface PagedList<T> {
  /** null until the first page of the current query has loaded. */
  items: T[] | null;
  error: string | null;
  /** A request for the current query is in flight. */
  pending: boolean;
  hasMore: boolean;
  loadMore: () => void;
  /** Reloads the first page and keeps the shown rows until it answers. */
  reload: () => void;
  /** Repeats whatever failed: the first page when nothing is shown, otherwise the next page. */
  retry: () => void;
}

/**
 * Cursor paging for one query. A new `fetchPage` (its filter changed) starts over with a fresh cursor and
 * id set; only the latest request may write state or release the in-flight lock, so a slow superseded
 * answer never overwrites the current list.
 */
export function usePagedList<T>(
  fetchPage: (cursor: string | null) => Promise<ApiResult<CursorPage<T>>>,
  idOf: (item: T) => string,
): PagedList<T> {
  const [items, setItems] = useState<T[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const seq = useRef(0);
  const inFlight = useRef(false);
  const cursor = useRef<string | null>(null);
  const seen = useRef<Set<string>>(new Set());
  const shown = useRef(false);

  const run = useCallback(
    async (append: boolean): Promise<void> => {
      const ticket = ++seq.current;
      inFlight.current = true;
      setPending(true);
      setError(null);
      try {
        const result = await fetchPage(append ? cursor.current : null);
        if (ticket !== seq.current) return;
        setError(errorMessageOf(result));
        if (!result.ok) return;
        const ids = append ? seen.current : new Set<string>();
        const fresh = result.data.items.filter((item) => {
          const id = idOf(item);
          if (ids.has(id)) return false;
          ids.add(id);
          return true;
        });
        seen.current = ids;
        cursor.current = result.data.next_cursor;
        shown.current = true;
        setHasMore(result.data.next_cursor !== null);
        setItems((prev) => (append && prev !== null ? [...prev, ...fresh] : fresh));
      } finally {
        if (ticket === seq.current) {
          inFlight.current = false;
          setPending(false);
        }
      }
    },
    [fetchPage, idOf],
  );

  useEffect(() => {
    cursor.current = null;
    seen.current = new Set();
    shown.current = false;
    setItems(null);
    setHasMore(false);
    void run(false);
    return () => {
      seq.current += 1;
      inFlight.current = false;
    };
  }, [run]);

  const loadMore = useCallback((): void => {
    if (inFlight.current || cursor.current === null) return;
    void run(true);
  }, [run]);

  const reload = useCallback((): void => {
    void run(false);
  }, [run]);

  const retry = useCallback((): void => {
    if (shown.current) loadMore();
    else reload();
  }, [loadMore, reload]);

  return { items, error, pending, hasMore, loadMore, reload, retry };
}
