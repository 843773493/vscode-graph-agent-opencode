function filePayload(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const result = String(reader.result || "");
      const commaIndex = result.indexOf(",");
      if (commaIndex < 0) {
        reject(new Error(`读取文件失败: ${file.name}`));
        return;
      }
      resolve({
        name: file.name,
        mimeType: file.type || "application/octet-stream",
        data: result.slice(commaIndex + 1),
      });
    });
    reader.addEventListener("error", () => reject(reader.error || new Error(`读取文件失败: ${file.name}`)));
    reader.readAsDataURL(file);
  });
}

export function createBrowserModalUi({
  modal,
  form,
  title,
  message,
  prompt,
  files,
  cancelButton,
  acceptButton,
  isAttached,
  sendIfAttached,
  setStatus,
}) {
  let currentKind = null;

  function sync(snapshot) {
    const dialog = snapshot.pending_dialog;
    const nextKind = dialog ? `dialog:${dialog.type}` : snapshot.pending_file_chooser ? "filechooser" : null;
    if (!nextKind) {
      currentKind = null;
      if (modal.open) {
        modal.close();
      }
      return;
    }

    if (dialog) {
      const labels = {
        alert: "页面提示",
        confirm: "页面确认",
        prompt: "页面输入",
        beforeunload: "离开页面？",
      };
      title.textContent = labels[dialog.type] || "页面对话框";
      message.textContent = dialog.message || "该页面需要你的确认。";
      prompt.hidden = dialog.type !== "prompt";
      files.hidden = true;
      acceptButton.textContent = "确定";
      cancelButton.textContent = dialog.type === "alert" ? "关闭" : "取消";
      if (currentKind !== nextKind) {
        prompt.value = dialog.defaultValue || "";
      }
    } else {
      title.textContent = "选择上传文件";
      message.textContent = "选择的文件会直接交给当前网页，不会复制到工作区。单次最多 20 个、合计 25 MiB。";
      prompt.hidden = true;
      files.hidden = false;
      acceptButton.textContent = "使用所选文件";
      cancelButton.textContent = "取消";
      if (currentKind !== nextKind) {
        files.value = "";
      }
    }
    currentKind = nextKind;
    if (!modal.open) {
      modal.showModal();
    }
  }

  async function submit(accept) {
    if (!currentKind || !isAttached()) {
      return;
    }
    acceptButton.disabled = true;
    cancelButton.disabled = true;
    try {
      if (currentKind === "filechooser") {
        const selectedFiles = accept ? [...files.files] : [];
        const totalBytes = selectedFiles.reduce((total, file) => total + file.size, 0);
        if (selectedFiles.length > 20 || totalBytes > 25 * 1024 * 1024) {
          throw new Error("单次最多选择 20 个文件，且合计不能超过 25 MiB");
        }
        sendIfAttached({
          type: "selectFiles",
          files: await Promise.all(selectedFiles.map(filePayload)),
        });
        setStatus(selectedFiles.length > 0 ? `正在上传 ${selectedFiles.length} 个文件...` : "已取消文件选择");
        return;
      }
      sendIfAttached({
        type: "handleDialog",
        accept,
        ...(prompt.hidden ? {} : { promptText: prompt.value }),
      });
      setStatus(accept ? "正在确认页面对话框..." : "正在取消页面对话框...");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error), true);
    } finally {
      acceptButton.disabled = false;
      cancelButton.disabled = false;
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submit(true);
  });
  cancelButton.addEventListener("click", () => void submit(false));
  modal.addEventListener("cancel", (event) => {
    event.preventDefault();
    void submit(false);
  });

  return { sync };
}
