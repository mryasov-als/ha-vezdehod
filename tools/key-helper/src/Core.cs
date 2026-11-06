using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Text.RegularExpressions;
using System.Threading;

namespace VezdehodKeyHelper
{
    public sealed class Credentials
    {
        public string Phone { get; private set; }
        public string Key { get; private set; }
        public Credentials(string phone, string key) { Phone = phone; Key = key; }
        public override string ToString() { return "Credentials [hidden]"; }
    }

    public static class CredentialParser
    {
        public static Credentials Parse(string line, decimal minimumEpoch)
        {
            if (String.IsNullOrEmpty(line) || line.Length > 32768) return null;
            Match stamp = Regex.Match(line, @"^\s*(\d{9,12}(?:\.\d{1,9})?)\s");
            decimal when;
            if (!stamp.Success || !Decimal.TryParse(stamp.Groups[1].Value,
                NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out when) || when < minimumEpoch)
                return null;
            Match request = Regex.Match(line, "^\\s*\\d{9,12}(?:\\.\\d{1,9})?\\s+\\d+\\s+\\d+\\s+[VDIWEF]\\s+okhttp\\.OkHttpClient\\s*:\\s*--> GET (https://[^\\s<>\"']+)");
            Uri uri;
            if (!request.Success || Regex.IsMatch(request.Groups[1].Value, "%(?![0-9a-fA-F]{2})") ||
                !Uri.TryCreate(request.Groups[1].Value, UriKind.Absolute, out uri)) return null;
            if (uri.Scheme != "https" || !uri.IsDefaultPort || uri.UserInfo.Length != 0 || uri.Fragment.Length != 0)
                return null;
            bool allowed =
                (uri.Host == "lk.comendant24.ru" && (uri.AbsolutePath == "/vezdehod/v1/get_facility" ||
                    uri.AbsolutePath == "/vezdehod/v1/check_auth")) ||
                (uri.Host == "client.comendant24.ru" && (uri.AbsolutePath == "/barrier/mobileapp/get_facility" ||
                    uri.AbsolutePath == "/barrier/mobileapp/check_auth"));
            if (!allowed) return null;
            Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.Ordinal);
            try
            {
                foreach (string item in uri.Query.TrimStart('?').Split('&'))
                {
                    int split = item.IndexOf('=');
                    if (split < 0) return null;
                    string name = Uri.UnescapeDataString(item.Substring(0, split).Replace("+", " "));
                    string value = Uri.UnescapeDataString(item.Substring(split + 1).Replace("+", " "));
                    if (values.ContainsKey(name)) return null;
                    values.Add(name, value);
                }
            }
            catch (UriFormatException) { return null; }
            string phone, key;
            if (!values.TryGetValue("auth_phone", out phone) || !values.TryGetValue("mobile_key", out key)) return null;
            phone = phone.Trim();
            if (phone.StartsWith("+", StringComparison.Ordinal)) phone = phone.Substring(1);
            if (!Regex.IsMatch(phone, "^[0-9]{10,15}$") || key.Length < 8 || key.Length > 512) return null;
            foreach (char ch in key) if (Char.IsControl(ch) || Char.IsWhiteSpace(ch)) return null;
            if (phone.Length == 11 && phone.StartsWith("8", StringComparison.Ordinal)) phone = "7" + phone.Substring(1);
            return new Credentials(phone, key);
        }
    }

    public sealed class CommandResult
    {
        public int ExitCode { get; private set; }
        public string Output { get; private set; }
        public string Error { get; private set; }
        public CommandResult(int code, string output, string error)
        { ExitCode = code; Output = output ?? ""; Error = error ?? ""; }
    }

    public interface IAdbRunner
    {
        CommandResult Run(string arguments, CancellationToken token, int timeoutMs);
    }

    public enum StatusKind { Preparing, WaitingForUsb, Authorization, WaitingForApp, Reading }
    public sealed class ReaderStatus
    {
        public StatusKind Kind { get; private set; }
        public string Message { get; private set; }
        public ReaderStatus(StatusKind kind, string message) { Kind = kind; Message = message; }
    }

    public sealed class ReaderException : Exception
    {
        public string Code { get; private set; }
        public ReaderException(string code, string message) : base(message) { Code = code; }
    }

    public sealed class CaptureEngine
    {
        public const string Package = "ru.hukutoc2288.vezdehod";
        private readonly IAdbRunner adb;
        private readonly Action<int, CancellationToken> pause;
        private StatusKind? previous;
        public CaptureEngine(IAdbRunner runner, Action<int, CancellationToken> delay)
        { adb = runner; pause = delay; }

        private void Notify(Action<ReaderStatus> report, StatusKind kind, string message)
        {
            if (previous == kind) return;
            previous = kind;
            report(new ReaderStatus(kind, message));
        }

        private static bool Disconnected(CommandResult result)
        {
            string text = result.Error.ToLowerInvariant();
            return text.Contains("no devices") || text.Contains("no device") || text.Contains("device offline") ||
                text.Contains("device not found") || text.Contains("unauthorized");
        }

        public Credentials Capture(CancellationToken token, Action<ReaderStatus> report)
        {
            previous = null;
            Stopwatch waiting = Stopwatch.StartNew();
            while (true)
            {
                token.ThrowIfCancellationRequested();
                CommandResult state = adb.Run("-d get-state", token, 8000);
                if (state.ExitCode == 0 && state.Output.Trim() == "device") break;
                if (state.Error.IndexOf("more than one", StringComparison.OrdinalIgnoreCase) >= 0)
                    throw new ReaderException("multiple_devices", "Подключено несколько USB-устройств. Оставьте подключённым один телефон.");
                if (state.Error.IndexOf("unauthorized", StringComparison.OrdinalIgnoreCase) >= 0)
                    Notify(report, StatusKind.Authorization, "Разблокируйте телефон и подтвердите «Разрешить отладку по USB».");
                else
                    Notify(report, StatusKind.WaitingForUsb, "Подключите телефон по USB. Включите «Отладку по USB» и разблокируйте экран.");
                if (waiting.Elapsed.TotalMinutes >= 3)
                    throw new ReaderException("usb_timeout", "Телефон не подключён. Проверьте кабель, режим USB и разрешение отладки, затем нажмите «Старт».");
                pause(700, token);
            }

            Notify(report, StatusKind.Preparing, "Телефон подключён. Подготавливаю поиск…");
            CommandResult app = adb.Run("-d shell pm path " + Package, token, 8000);
            if (Disconnected(app)) throw new ReaderException("disconnected", "Телефон отключился. Подключите его и повторите поиск.");
            if (app.ExitCode != 0 || !app.Output.TrimStart().StartsWith("package:", StringComparison.Ordinal))
                throw new ReaderException("app_missing", "На телефоне не найдено официальное приложение «Вездеход».");
            CommandResult clock = adb.Run("-d shell date +%s", token, 8000);
            decimal deviceSeconds;
            if (clock.ExitCode != 0 || !Decimal.TryParse(clock.Output.Trim(), NumberStyles.Integer,
                CultureInfo.InvariantCulture, out deviceSeconds) || deviceSeconds < 1000000000m)
                throw new ReaderException("device_clock", "Не удалось прочитать время телефона. Повторите подключение.");
            // Ignore all old buffered messages, including the current partial second.
            decimal minimumEpoch = deviceSeconds + 1m;
            pause(1100, token);
            waiting.Restart();
            while (waiting.Elapsed.TotalMinutes < 2)
            {
                token.ThrowIfCancellationRequested();
                Notify(report, StatusKind.WaitingForApp, "Откройте список шлагбаумов в «Вездеходе».");
                CommandResult process = adb.Run("-d shell pidof " + Package, token, 8000);
                if (Disconnected(process)) throw new ReaderException("disconnected", "Телефон отключился. Подключите его и повторите поиск.");
                string pid = process.Output.Trim();
                if (pid.IndexOf(' ') >= 0 || pid.IndexOf('\n') >= 0)
                    throw new ReaderException("multiple_app_processes", "Найдено несколько копий «Вездехода». Закройте копию в другом профиле телефона.");
                if (Regex.IsMatch(pid, "^[0-9]{1,10}$"))
                {
                    CommandResult logs = adb.Run("-d logcat -b main -d -v epoch -t 500 --pid=" + pid + " okhttp.OkHttpClient:I *:S", token, 8000);
                    if (Disconnected(logs)) throw new ReaderException("disconnected", "Телефон отключился. Подключите его и повторите поиск.");
                    if (logs.ExitCode != 0)
                        throw new ReaderException("log_unavailable", "Телефон не разрешил чтение журнала этим способом.");
                    foreach (string line in logs.Output.Split('\n'))
                    {
                        token.ThrowIfCancellationRequested();
                        Credentials credentials = CredentialParser.Parse(line, minimumEpoch);
                        if (credentials != null) return credentials;
                    }
                }
                pause(700, token);
            }
            throw new ReaderException("no_data", "Данные не найдены. Нажмите «Старт» и откройте список шлагбаумов в «Вездеходе». Эта версия приложения может не записывать ключ в журнал.");
        }
    }
}
