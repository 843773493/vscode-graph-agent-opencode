import { t } from "../../../../shared/core/i18n/index.js";
import MarkdownContent from "../chat/MarkdownContent";
import type { useUserQuestions } from "../../hooks/runtime/useUserQuestions";

export default function ComposerQuestions(questions: ReturnType<typeof useUserQuestions>) {
  return <div className="composer-questions" aria-busy={questions.loading}>
    <p className="composer-questions-hint">{t("composer.questions.hint")}</p>
    <div role="list">
      {questions.items.map(item => <article role="listitem" className="composer-question" key={item.question_id}>
        <div className="composer-question-heading"><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time>
          <button type="button" className="composer-status-icon" disabled={Boolean(questions.removing)}
            aria-label={t("composer.questions.delete")} title={t("composer.questions.delete")}
            onClick={() => void questions.remove(item.question_id)}><span className="codicon codicon-trash" aria-hidden="true" /></button>
        </div>
        <MarkdownContent value={item.markdown} />
      </article>)}
    </div>
    {questions.hasMore ? <button type="button" disabled={questions.loading} onClick={() => void questions.loadMore()}>{t("composer.questions.more")}</button> : null}
    {!questions.loading && questions.count > 0 && !questions.items.length ? <button type="button" onClick={questions.retry}>{t("composer.questions.retry")}</button> : null}
    {questions.loading ? <span className="codicon codicon-loading codicon-modifier-spin" aria-label={t("composer.questions.loading")} /> : null}
  </div>;
}
