namespace DemoApi.Logging;

public class RequestLogEntry
{
    public DateTime Timestamp { get; set; }
    public string Endpoint { get; set; } = string.Empty;
    public string Method { get; set; } = string.Empty;
    public int StatusCode { get; set; }
    public string? UserId { get; set; }
    public long ResponseTimeMs { get; set; }
    public string RequestId { get; set; } = string.Empty;
    public int? SimulatedLatencyMs { get; set; }
}
