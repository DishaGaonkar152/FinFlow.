from mcp.server.fastmcp import FastMCP

from .sec import SecClient


mcp = FastMCP("financial-filing-auditor")
sec = SecClient()


@mcp.tool(name="retrieve_10k_filing", description="Retrieve the latest available SEC EDGAR Form 10-K and company details by ticker, exact company name, or CIK; DEMO works offline.")
def retrieve_10k_filing(ticker: str) -> dict:
    filing = sec.retrieve_filing(ticker)
    return {
        "company": filing.company,
        "accession": filing.accession,
        "report_date": filing.report_date,
        "url": filing.url,
        "html": filing.html,
        "offline": filing.offline,
    }


@mcp.tool(name="lookup_xbrl_company_facts", description="Retrieve SEC EDGAR XBRL Company Facts used as independent financial ground truth.")
def lookup_xbrl_company_facts(ticker: str) -> dict:
    return {"ticker": ticker.upper(), "company_facts": sec.lookup_company_facts(ticker)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
