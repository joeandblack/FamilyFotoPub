// Runs the installer for the setup dialog (setup-dialog.ps1): its output is read on background
// threads into a queue, which the window's timer reads on the window's own thread. A password is
// written to the child's standard input and not kept: a new library's account password as one line,
// a backup's administrator password as one frame (app.backup.passwords.frame).
using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Text;
public class FamilyFotoSetupRun {
    public ConcurrentQueue<string> Lines = new ConcurrentQueue<string>();
    private Process process;
    private StreamWriter log;
    private readonly object writing = new object();
    // Windows command-line quoting: backslashes are literal unless they come before a quote.
    public static string Quote(string value) {
        var quoted = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { quoted.Append('\\', slashes * 2 + 1); quoted.Append('"'); }
            else { quoted.Append('\\', slashes); quoted.Append(c); }
            slashes = 0;
        }
        quoted.Append('\\', slashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }
    private void Take(string line, bool error) {
        if (line == null) return;
        string text = error ? "! " + line : line;
        Lines.Enqueue(text);
        lock (writing) { log.WriteLine(text); log.Flush(); }
    }
    // One backup-password frame: a 4-byte big-endian length, then {"password": "..."} in UTF-8.
    public static byte[] Frame(string password) {
        var json = new StringBuilder("{\"password\": \"");
        foreach (char c in password) {
            if (c == '"' || c == '\\') { json.Append('\\'); json.Append(c); }
            else if (c < ' ') { json.AppendFormat("\\u{0:x4}", (int)c); }
            else { json.Append(c); }
        }
        json.Append("\"}");
        byte[] payload = new UTF8Encoding(false).GetBytes(json.ToString());
        if (payload.Length > 8192) throw new ArgumentException("The password is too long.");
        byte[] frame = new byte[4 + payload.Length];
        frame[0] = (byte)(payload.Length >> 24); frame[1] = (byte)(payload.Length >> 16);
        frame[2] = (byte)(payload.Length >> 8); frame[3] = (byte)payload.Length;
        Array.Copy(payload, 0, frame, 4, payload.Length);
        return frame;
    }
    public void StartFramed(string program, string[] arguments, string directory, byte[] frame, string logPath) {
        Launch(program, arguments, directory, logPath);
        try { process.StandardInput.BaseStream.Write(frame, 0, frame.Length); process.StandardInput.BaseStream.Flush(); }
        finally { process.StandardInput.BaseStream.Close(); Array.Clear(frame, 0, frame.Length); }
    }
    public void Start(string program, string[] arguments, string directory, string input, string logPath) {
        Launch(program, arguments, directory, logPath);
        // Do not use the locale-dependent text writer, even to close a raw pipe:
        // its encoder may emit a BOM on close, corrupting a password frame.
        byte[] bytes = input == null ? new byte[0] : new UTF8Encoding(false, true).GetBytes(input + "\n");
        try { process.StandardInput.BaseStream.Write(bytes, 0, bytes.Length); process.StandardInput.BaseStream.Flush(); }
        finally { process.StandardInput.BaseStream.Close(); Array.Clear(bytes, 0, bytes.Length); }
    }
    private void Launch(string program, string[] arguments, string directory, string logPath) {
        log = new StreamWriter(logPath, true, new UTF8Encoding(false));
        var info = new ProcessStartInfo(program);
        var joined = new StringBuilder();
        foreach (var argument in arguments) { if (joined.Length > 0) joined.Append(' '); joined.Append(Quote(argument)); }
        info.Arguments = joined.ToString();
        info.WorkingDirectory = directory;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        process = new Process();
        process.StartInfo = info;
        process.OutputDataReceived += (sender, e) => Take(e.Data, false);
        process.ErrorDataReceived += (sender, e) => Take(e.Data, true);
        // .NET Framework chooses the stdin writer from Console.InputEncoding and
        // may emit its preamble during Start(), before any raw bytes are written.
        var previousInputEncoding = Console.InputEncoding;
        try { Console.InputEncoding = new UTF8Encoding(false); process.Start(); }
        finally { Console.InputEncoding = previousInputEncoding; }
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
    }
    public void Stop() { try { if (process != null && !process.HasExited) process.Kill(); } catch (InvalidOperationException) { } }
    public bool Finished {
        get {
            if (process == null || !process.HasExited) return false;
            process.WaitForExit();  // Lets the last output lines arrive.
            return true;
        }
    }
    public int ExitCode { get { return process.ExitCode; } }
    public void Close() { lock (writing) { if (log != null) { log.Dispose(); log = null; } } }
}
