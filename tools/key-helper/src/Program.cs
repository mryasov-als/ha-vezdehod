using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: AssemblyTitle("Вездеход — получение ключа")]
[assembly: AssemblyProduct("Вездеход Key Helper")]
[assembly: AssemblyVersion("0.1.1.0")]
[assembly: AssemblyFileVersion("0.1.1.0")]

namespace VezdehodKeyHelper
{
    internal static class Program
    {
        [STAThread]
        public static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            if (args.Length == 2 && args[0] == "--self-check")
            {
                try
                {
                    string adb = BundledAdb.Ensure(CancellationToken.None);
                    CommandResult result = new ProcessRunner(adb).Run("version", CancellationToken.None, 8000);
                    if (result.ExitCode != 0 || !result.Output.Contains("Android Debug Bridge version")) return 2;
                    File.WriteAllText(args[1], "ok=true\nadb=" + BundleInfo.AdbRevision + "\nfiles=" + BundleInfo.Files.Count,
                        new UTF8Encoding(false));
                    return 0;
                }
                catch { return 1; }
            }
            if (args.Length == 2 && args[0] == "--preview")
            {
                try { using (MainWindow window = new MainWindow()) window.SavePreview(args[1]); return 0; }
                catch { return 1; }
            }
            Application.ThreadException += delegate {
                MessageBox.Show("Не удалось выполнить действие. Закройте программу и запустите снова.",
                    "Вездеход", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            };
            Application.Run(new MainWindow());
            return 0;
        }
    }

    public sealed class MainWindow : Form
    {
        private readonly Label status;
        private readonly Label resultNote;
        private readonly Panel statusPanel;
        private readonly Button start;
        private readonly TextBox phone;
        private readonly TextBox key;
        private readonly Button copyPhone;
        private readonly Button copyKey;
        private readonly CheckBox showKey;
        private readonly NotifyIcon tray;
        private readonly System.Windows.Forms.Timer trayTimer;
        private CancellationTokenSource active;
        private bool closing;
        private readonly Color accent = Color.FromArgb(233, 120, 62);

        [DllImport("user32.dll")] private static extern bool DestroyIcon(IntPtr handle);

        private static Label TextLabel(string text, int x, int y, int width, int height, float size, bool bold)
        {
            return new Label { Text = text, Left = x, Top = y, Width = width, Height = height,
                Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular),
                ForeColor = Color.FromArgb(40, 45, 50), Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
        }

        private static Button SmallButton(string text, int x, int y)
        {
            return new Button { Text = text, Left = x, Top = y, Width = 130, Height = 32,
                Font = new Font("Segoe UI", 10), Anchor = AnchorStyles.Top | AnchorStyles.Right };
        }

        public MainWindow()
        {
            Text = "Вездеход — получить ключ";
            ClientSize = new Size(720, 690);
            MinimumSize = new Size(670, 730);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.White;
            AutoScaleMode = AutoScaleMode.Dpi;
            Font = new Font("Segoe UI", 10);
            Icon = SystemIcons.Application;
            using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("Vezdehod.Icon.png"))
            using (Image image = Image.FromStream(resource))
            {
                Bitmap logo = new Bitmap(image);
                PictureBox picture = new PictureBox { Left = 24, Top = 26, Width = 44, Height = 44,
                    Image = logo, SizeMode = PictureBoxSizeMode.Zoom };
                Controls.Add(picture);
                IntPtr handle = logo.GetHicon();
                using (Icon borrowed = Icon.FromHandle(handle)) Icon = (Icon)borrowed.Clone();
                DestroyIcon(handle);
            }
            Controls.Add(TextLabel("Получить ключ для Home Assistant", 84, 24, 610, 38, 18, true));
            Controls.Add(TextLabel("Скопируйте данные действующего входа в «Вездеход».\nВходить заново и получать SMS не требуется.",
                24, 84, 672, 50, 11, false));
            Controls.Add(TextLabel("1. Включите «Отладку по USB» на телефоне.\n2. Подключите телефон кабелем и нажмите «Старт».\n3. Разрешите доступ на телефоне и откройте список шлагбаумов в «Вездеходе».",
                24, 148, 672, 76, 10.5f, false));
            LinkLabel help = new LinkLabel { Text = "Как включить USB-отладку?", Left = 24, Top = 226,
                Width = 330, Height = 26, LinkColor = Color.FromArgb(30, 110, 145) };
            help.LinkClicked += delegate { ShowHelp(); };
            Controls.Add(help);
            LinkLabel about = new LinkLabel { Text = "О программе и лицензиях", Left = 468, Top = 226,
                Width = 228, Height = 26, TextAlign = ContentAlignment.TopRight,
                Anchor = AnchorStyles.Top | AnchorStyles.Right, LinkColor = Color.FromArgb(30, 110, 145) };
            about.LinkClicked += delegate {
                if (MessageBox.Show(this, "Вездеход Key Helper 0.1.1\nВстроенный ADB " + BundleInfo.AdbRevision +
                    "\n\nПрограмма ищет данные в журнале только подключённого USB-телефона.\n\nПоказать лицензии встроенных компонентов?",
                    "О программе", MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
                {
                    try { BundledAdb.OpenNotice(); }
                    catch (Exception) { MessageBox.Show(this, "Не удалось открыть текст лицензий.", "Вездеход"); }
                }
            };
            Controls.Add(about);
            statusPanel = new Panel { Left = 24, Top = 266, Width = 672, Height = 88,
                BackColor = Color.FromArgb(239, 244, 248), Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
            status = TextLabel("Готово к подключению.\nНажмите «Старт», когда телефон будет рядом.", 16, 12, 640, 68, 11, false);
            statusPanel.Controls.Add(status);
            Controls.Add(statusPanel);
            start = new Button { Left = 24, Top = 372, Width = 200, Height = 44, Text = "Старт",
                BackColor = accent, ForeColor = Color.White, FlatStyle = FlatStyle.Flat,
                Font = new Font("Segoe UI", 12, FontStyle.Bold) };
            start.FlatAppearance.BorderSize = 0;
            start.Click += async delegate { await StartOrStop(); };
            Controls.Add(start);
            Controls.Add(TextLabel("После находки поиск остановится автоматически.\nПрограмма не отправляет номер и ключ в интернет.",
                242, 374, 454, 44, 9.5f, false));
            GroupBox results = new GroupBox { Text = "Найденные данные", Left = 24, Top = 434,
                Width = 672, Height = 148, Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
            results.Controls.Add(TextLabel("Телефон", 14, 32, 90, 30, 10.5f, false));
            results.Controls.Add(TextLabel("Ключ", 14, 79, 90, 30, 10.5f, false));
            phone = new TextBox { Left = 118, Top = 29, Width = 390, ReadOnly = true, BackColor = Color.White,
                Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
            key = new TextBox { Left = 118, Top = 76, Width = 390, ReadOnly = true, BackColor = Color.White,
                UseSystemPasswordChar = true, Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right };
            copyPhone = SmallButton("Копировать", 524, 27);
            copyKey = SmallButton("Копировать", 524, 74);
            copyPhone.Enabled = copyKey.Enabled = false;
            copyPhone.Click += delegate { Copy(phone.Text, "Номер скопирован."); };
            copyKey.Click += delegate { Copy(key.Text, "Ключ скопирован."); };
            showKey = new CheckBox { Text = "Показать ключ", Left = 118, Top = 113, Width = 220, Height = 25,
                Font = new Font("Segoe UI", 9), Enabled = false };
            showKey.CheckedChanged += delegate { key.UseSystemPasswordChar = !showKey.Checked; };
            results.Controls.AddRange(new Control[] { phone, key, copyPhone, copyKey, showKey });
            Controls.Add(results);
            resultNote = TextLabel("", 24, 594, 672, 27, 10, false);
            Controls.Add(resultNote);
            Label footer = TextLabel("0.1.1 · Перенос данных «Вездехода» в Home Assistant.\nПрограмма не сохраняет номер и ключ в файлы. Копирование — по кнопке.",
                24, 634, 672, 43, 9, false);
            footer.ForeColor = Color.FromArgb(105, 110, 115);
            Controls.Add(footer);
            tray = new NotifyIcon { Icon = Icon, Text = "Вездеход: получение ключа", Visible = false };
            tray.DoubleClick += delegate { Show(); WindowState = FormWindowState.Normal; Activate(); };
            trayTimer = new System.Windows.Forms.Timer { Interval = 6000 };
            trayTimer.Tick += delegate { tray.Visible = false; trayTimer.Stop(); };
            FormClosing += delegate { closing = true; if (active != null) active.Cancel(); tray.Visible = false; };
        }

        private void ShowHelp()
        {
            MessageBox.Show(this,
                "1. Откройте настройки телефона → «О телефоне».\n" +
                "2. Нажмите 7 раз на «Номер сборки». На Xiaomi — на версию MIUI/HyperOS.\n" +
                "3. Найдите раздел «Для разработчиков» и включите «Отладка по USB».\n" +
                "4. Подключите телефон кабелем с передачей данных и нажмите «Старт» в программе.\n" +
                "5. Разблокируйте телефон и разрешите отладку с этого компьютера.\n\n" +
                "Пункты меню зависят от телефона. На Windows может потребоваться USB-драйвер производителя.\n\n" +
                "После получения ключа USB-отладку можно выключить. Не выходите из аккаунта «Вездехода» и не запрашивайте новую SMS.",
                "Подключение телефона", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private void SetStatus(string text, bool success, bool error)
        {
            status.Text = text;
            statusPanel.BackColor = success ? Color.FromArgb(231, 246, 236) :
                error ? Color.FromArgb(255, 239, 232) : Color.FromArgb(239, 244, 248);
        }

        private void Report(ReaderStatus update)
        {
            if (closing || IsDisposed) return;
            try { BeginInvoke((Action)delegate { if (!closing) SetStatus(update.Message, false, false); }); }
            catch (InvalidOperationException) { }
        }

        private void ClearResult()
        {
            phone.Clear(); key.Clear(); showKey.Checked = false;
            copyPhone.Enabled = copyKey.Enabled = showKey.Enabled = false;
            resultNote.Text = "";
        }

        private void Found(Credentials credentials, bool notify)
        {
            phone.Text = credentials.Phone;
            key.Text = credentials.Key;
            copyPhone.Enabled = copyKey.Enabled = showKey.Enabled = true;
            SetStatus("Данные найдены. Поиск остановлен.\nСкопируйте номер и ключ в настройку интеграции Home Assistant.", true, false);
            resultNote.Text = "Ключ будет проверен при добавлении интеграции в Home Assistant.";
            resultNote.ForeColor = Color.FromArgb(45, 110, 68);
            if (notify)
            {
                tray.Visible = true;
                tray.ShowBalloonTip(5000, "Данные «Вездехода» найдены", "Поиск остановлен. Вернитесь в программу, чтобы скопировать данные.", ToolTipIcon.Info);
                trayTimer.Start();
            }
        }

        private async Task StartOrStop()
        {
            if (active != null)
            {
                start.Enabled = false;
                SetStatus("Останавливаю поиск…", false, false);
                active.Cancel();
                return;
            }
            ClearResult();
            trayTimer.Stop(); tray.Visible = false;
            active = new CancellationTokenSource();
            CancellationToken token = active.Token;
            start.Text = "Стоп";
            SetStatus("Подготавливаю подключение…", false, false);
            try
            {
                Credentials credentials = await Task.Run(delegate {
                    string adb = BundledAdb.Ensure(token);
                    CaptureEngine engine = new CaptureEngine(new ProcessRunner(adb), delegate(int ms, CancellationToken ct) {
                        if (ct.WaitHandle.WaitOne(ms)) ct.ThrowIfCancellationRequested();
                    });
                    return engine.Capture(token, Report);
                });
                if (!closing) Found(credentials, true);
            }
            catch (OperationCanceledException) { if (!closing) SetStatus("Поиск остановлен.", false, false); }
            catch (ReaderException ex) { if (!closing) SetStatus(ex.Message, false, true); }
            catch (Exception) { if (!closing) SetStatus("Не удалось завершить поиск. Проверьте подключение телефона и запустите его снова.", false, true); }
            finally
            {
                active.Dispose(); active = null;
                if (!closing) { start.Text = "Старт"; start.Enabled = true; }
            }
        }

        private void Copy(string value, string message)
        {
            if (String.IsNullOrEmpty(value)) return;
            try { Clipboard.SetText(value); resultNote.Text = message; }
            catch (ExternalException) { resultNote.Text = "Буфер обмена занят. Нажмите «Копировать» ещё раз."; }
        }

        public void SavePreview(string path)
        {
            Found(new Credentials("79990001122", "fixture-key-only-123"), false);
            ShowInTaskbar = false;
            Opacity = 0;
            StartPosition = FormStartPosition.Manual;
            Location = new Point(-32000, -32000);
            Show();
            PerformLayout();
            Application.DoEvents();
            using (Bitmap bitmap = new Bitmap(Width, Height))
            {
                DrawToBitmap(bitmap, new Rectangle(0, 0, Width, Height));
                int colored = 0;
                for (int y = 90; y < Height - 40; y += 8)
                    for (int x = 40; x < Width - 40; x += 8)
                    {
                        Color pixel = bitmap.GetPixel(x, y);
                        if (pixel.R < 230 || pixel.G < 230 || pixel.B < 230) colored++;
                    }
                if (colored < 100) throw new InvalidOperationException("Preview content is blank");
                bitmap.Save(path, System.Drawing.Imaging.ImageFormat.Png);
            }
            Hide();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                if (active != null) active.Cancel();
                if (trayTimer != null) trayTimer.Dispose();
                if (tray != null) tray.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
